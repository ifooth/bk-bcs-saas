# -*- coding: utf-8 -*-
#
# Tencent is pleased to support the open source community by making 蓝鲸智云PaaS平台社区版 (BlueKing PaaS Community Edition) available.
# Copyright (C) 2017-2019 THL A29 Limited, a Tencent company. All rights reserved.
# Licensed under the MIT License (the "License"); you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://opensource.org/licenses/MIT
#
# Unless required by applicable law or agreed to in writing, software distributed under the License is distributed on
# an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the License for the
# specific language governing permissions and limitations under the License.
#
import datetime
import copy
import logging
import json
from itertools import groupby

from rest_framework import serializers, viewsets
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.renderers import BrowsableAPIRenderer

from backend.utils.renderers import BKAPIRenderer
from backend.accounts import bcs_perm
from backend.utils.errcodes import ErrorCode
from backend.apps.application.utils import APIResponse
from backend.apps.application.base_views import BaseAPI
from backend.apps.configuration.models import Template, Application, VersionedEntity, Service, ShowVersion, K8sService
from backend.components import paas_cc
from backend.components.bcs import k8s, mesos
from backend.apps import constants
from backend.apps.network.utils import (handle_lb, get_lb_status, delete_lb_by_bcs, get_namespace_name)
from backend.apps.instance.constants import (LABLE_TEMPLATE_ID, LABLE_INSTANCE_ID, SEVICE_SYS_CONFIG,
                                             ANNOTATIONS_CREATOR, ANNOTATIONS_UPDATOR, ANNOTATIONS_CREATE_TIME,
                                             ANNOTATIONS_UPDATE_TIME, ANNOTATIONS_WEB_CACHE, K8S_SEVICE_SYS_CONFIG,
                                             PUBLIC_LABELS, PUBLIC_ANNOTATIONS, SOURCE_TYPE_LABEL_KEY)
from backend.apps.instance.generator import (handel_service_db_config, handel_k8s_service_db_config, get_bcs_context,
                                             handle_webcache_config, remove_key, handle_k8s_api_version)
from backend.apps.instance.utils_pub import get_cluster_version
from backend.apps.configuration.serializers import ServiceCreateOrUpdateSLZ, K8sServiceCreateOrUpdateSLZ
from backend.apps.instance.drivers import get_scheduler_driver
from backend.apps.instance.funutils import update_nested_dict, render_mako_context
from backend.apps.instance.models import InstanceConfig
from backend.utils.exceptions import ComponentError
from backend.activity_log import client as activity_client
from backend.apps.application.constants import DELETE_INSTANCE
from backend.apps.network.serializers import (
    BatchResourceSLZ, LoadBalancesSLZ, UpdateLoadBalancesSLZ, GetLoadBalanceSLZ
)
from backend.apps.network.models import MesosLoadBlance
from backend.utils.error_codes import error_codes
from backend.apps.application.constants import SOURCE_TYPE_MAP
# from backend.apps.network import serializers

logger = logging.getLogger(__name__)
DEFAULT_ERROR_CODE = ErrorCode.UnknownError


class Services(viewsets.ViewSet, BaseAPI):
    def get_lastest_ver_by_tempate_id(self, templat_id):
        """获取模板的最新可见版本
        """
        pass

    def get_services_by_cluster_id(self, request, params, project_id, cluster_id, project_kind=2):
        """查询services
        """
        access_token = request.user.token.access_token
        if project_kind == 2:
            client = mesos.MesosClient(
                access_token, project_id, cluster_id, env=None)
            resp = client.get_services(params)
        else:
            client = k8s.K8SClient(
                access_token, project_id, cluster_id, env=None)
            resp = client.get_service(params)

        if resp.get("code") != ErrorCode.NoError:
            logger.error(u"bcs_api error: %s" % resp.get("message", ""))
            return resp.get("code", DEFAULT_ERROR_CODE), resp.get("message", u"请求出现异常!")

        return ErrorCode.NoError, resp.get("data", [])

    def get_service_info(self, request, project_id, cluster_id, namespace, name):  # noqa
        """获取单个 service 的信息
        """
        flag, project_kind = self.get_project_kind(request, project_id)
        if not flag:
            return project_kind

        access_token = request.user.token.access_token
        params = {
            "env": "mesos" if project_kind == 2 else "k8s",
            "namespace": namespace,
            "name": name,
        }
        if project_kind == 2:
            client = mesos.MesosClient(
                access_token, project_id, cluster_id, env=None)
            resp = client.get_services(params)
            # 跳转到模板集页面需要的参数
            template_cate = 'mesos'
            relate_app_cate = 'application'
        else:
            client = k8s.K8SClient(
                access_token, project_id, cluster_id, env=None)
            resp = client.get_service(params)
            template_cate = 'k8s'
            relate_app_cate = 'deployment'

        if resp.get("code") != ErrorCode.NoError:
            raise ComponentError(resp.get("message"))

        resp_data = resp.get("data", [])
        if not resp_data:
            return APIResponse({
                "code": 400,
                "message": u"查询不到 Service[%s] 的信息" % name
            })
        s_data = resp_data[0].get('data', {})
        labels = s_data.get('metadata', {}).get('labels') or {}

        # 获取命名空间的id
        namespace_res = paas_cc.get_namespace_list(
            access_token, project_id, limit=constants.ALL_LIMIT)
        namespace_data = namespace_res.get('data', {}).get('results') or []
        namespace_dict = {i['name']: i['id'] for i in namespace_data}
        namespace = s_data.get('metadata', {}).get('namespace')
        namespace_id = namespace_dict.get(namespace)

        instance_id = labels.get(LABLE_INSTANCE_ID)

        # 是否关联LB
        lb_balance = labels.get('BCSBALANCE')
        if lb_balance:
            s_data['isLinkLoadBalance'] = True
            s_data['metadata']['lb_labels'] = {'BCSBALANCE': lb_balance}
        else:
            s_data['isLinkLoadBalance'] = False
        lb_name = labels.get('BCSGROUP')

        # 获取模板集信息
        template_id = labels.get(LABLE_TEMPLATE_ID)
        try:
            lasetest_ver = ShowVersion.objects.filter(
                template_id=template_id).order_by('-updated').first()
            show_version_name = lasetest_ver.name
            version_id = lasetest_ver.real_version_id
            version_entity = VersionedEntity.objects.get(id=version_id)
        except Exception:
            return APIResponse({
                "code": 400,
                "message": u"模板集[id:%s]没有可用的版本，无法更新service" % template_id
            })

        entity = version_entity.get_entity()

        # 获取更新人和创建人
        annotations = s_data.get('metadata', {}).get('annotations', {})
        creator = annotations.get(ANNOTATIONS_CREATOR, '')
        updator = annotations.get(ANNOTATIONS_UPDATOR, '')
        create_time = annotations.get(ANNOTATIONS_CREATE_TIME, '')
        update_time = annotations.get(ANNOTATIONS_UPDATE_TIME, '')

        # k8s 更新需要获取版本号
        resource_version = s_data.get(
            'metadata', {}).get('resourceVersion') or ''

        web_cache = annotations.get(ANNOTATIONS_WEB_CACHE)
        if not web_cache:
            # 备注中无，则从模板中获取，兼容mesos之前实例化过的模板数据
            _services = entity.get('service') if entity else None
            _services_id_list = _services.split(',') if _services else []
            _s = Service.objects.filter(
                id__in=_services_id_list, name=name).first()
            try:
                web_cache = _s.get_config.get('webCache')
            except Exception:
                pass
        else:
            try:
                web_cache = json.loads(web_cache)
            except Exception:
                pass
        s_data['webCache'] = web_cache
        deploy_tag_list = web_cache.get('deploy_tag_list') or []

        app_weight = {}
        if project_kind == 2:
            # 处理 mesos 中Service的关联数据
            apps = entity.get('application') if entity else None
            application_id_list = apps.split(',') if apps else []

            apps = Application.objects.filter(id__in=application_id_list)
            if apps:
                # 关联应用的权重
                for key in labels:
                    if key.startswith('BCS-WEIGHT-'):
                        app_name = key[11:]
                        _app = apps.filter(name=app_name).first()
                        if _app:
                            weight = int(labels[key])
                            app_weight[_app.app_id] = weight
        else:
            # 处理 k8s 中Service的关联数据
            if not deploy_tag_list:
                _servs = entity.get('K8sService') if entity else None
                _serv_id_list = _servs.split(',') if _servs else []
                _k8s_s = K8sService.objects.filter(
                    id__in=_serv_id_list, name=name).first()
                if _k8s_s:
                    deploy_tag_list = _k8s_s.get_deploy_tag_list()

        # 标签 和 备注 去除后台自动添加的
        or_annotations = s_data.get('metadata', {}).get('annotations', {})
        or_labels = s_data.get('metadata', {}).get('labels', {})
        if or_labels:
            pub_keys = PUBLIC_LABELS.keys()
            show_labels = {key: or_labels[key]
                           for key in or_labels if key not in pub_keys}
            s_data['metadata']['labels'] = show_labels
        if or_annotations:
            pub_an_keys = PUBLIC_ANNOTATIONS.keys()
            show_annotations = {key: or_annotations[key]
                                for key in or_annotations if key not in pub_an_keys}
            remove_key(show_annotations, ANNOTATIONS_WEB_CACHE)
            s_data['metadata']['annotations'] = show_annotations

        return APIResponse({
            "data": {
                'service': [{
                    'name': name,
                    'app_id': app_weight.keys(),
                    'app_weight': app_weight,
                    'deploy_tag_list': deploy_tag_list,
                    'config': s_data,
                    'version': version_id,
                    'lb_name': lb_name,
                    'instance_id': instance_id,
                    'namespace_id': namespace_id,
                    'cluster_id': cluster_id,
                    'namespace': namespace,
                    'creator': creator,
                    'updator': updator,
                    'create_time': create_time,
                    'update_time': update_time,
                    'show_version_name': show_version_name,
                    'resource_version': resource_version,
                    'template_id': template_id,
                    'template_cate': template_cate,
                    'relate_app_cate': relate_app_cate,
                }]
            }
        })

    def get(self, request, project_id):
        """ 获取项目下所有的服务 """
        # 获取kind

        logger.debug("get project kind: %s" % project_id)
        flag, project_kind = self.get_project_kind(request, project_id)
        if not flag:
            return project_kind

        logger.debug("get project clusters: %s" % project_id)
        cluster_dicts = self.get_project_cluster_info(request, project_id)
        cluster_data = cluster_dicts.get('results', {}) or {}

        params = dict(request.GET.items())
        params.update({
            "env": "mesos" if project_kind == 2 else "k8s",
        })

        data = []

        access_token = request.user.token.access_token
        cluster = paas_cc.get_all_clusters(
            access_token, project_id, limit=constants.ALL_LIMIT)
        cluster = cluster.get('data', {}).get('results') or []
        cluster = {i['cluster_id']: i['name'] for i in cluster}

        # 获取命名空间的id
        namespace_res = paas_cc.get_namespace_list(
            access_token, project_id, limit=constants.ALL_LIMIT)
        namespace_data = namespace_res.get('data', {}).get('results') or []
        namespace_dict = {i['name']: i['id'] for i in namespace_data}

        # 项目下的所有模板集id
        all_template_id_list = Template.objects.filter(project_id=project_id).values_list('id', flat=True)
        all_template_id_list = [str(template_id) for template_id in all_template_id_list]
        skip_namespace_list = constants.K8S_SYS_NAMESPACE
        skip_namespace_list.extend(constants.K8S_PLAT_NAMESPACE)
        for cluster_info in cluster_data:
            cluster_id = cluster_info.get('cluster_id')
            cluster_name = cluster_info.get('name')
            code, cluster_services = self.get_services_by_cluster_id(
                request, params, project_id, cluster_id, project_kind=project_kind)
            if code != ErrorCode.NoError:
                continue
            for _s in cluster_services:
                _config = _s.get('data', {})
                annotations = _config.get(
                    'metadata', {}).get('annotations', {})
                _s['update_time'] = annotations.get(
                    ANNOTATIONS_UPDATE_TIME, '')
                _s['updator'] = annotations.get(ANNOTATIONS_UPDATOR, '')
                _s['cluster_name'] = cluster_name
                _s['status'] = 'Running'
                _s['environment'] = cluster_info.get('environment')

                _s['can_update'] = True
                _s['can_update_msg'] = ''
                _s['can_delete'] = True
                _s['can_delete_msg'] = ''

                namespace_id = namespace_dict.get(_s['namespace'])
                _s['namespace_id'] = namespace_id

                labels = _config.get('metadata', {}).get('labels', {})
                template_id = labels.get(LABLE_TEMPLATE_ID)
                # 资源来源
                source_type = labels.get(SOURCE_TYPE_LABEL_KEY)
                if not source_type:
                    source_type = "template" if template_id else "other"
                _s['source_type'] = SOURCE_TYPE_MAP.get(source_type)

                # 处理 k8s 的系统命名空间的数据
                if project_kind == 1 and _s['namespace'] in skip_namespace_list:
                    _s['can_update'] = _s['can_delete'] = False
                    _s['can_update_msg'] = _s['can_delete_msg'] = u"不允许操作系统命名空间"
                    continue

                # 非模板集创建，可以删除但是不可以更新
                _s['can_update'] = False
                _s['can_update_msg'] = u"所属模板集不存在，无法操作"
                if template_id and template_id in all_template_id_list:
                    _s['can_update'] = True
                    _s['can_update_msg'] = ''

                # if template_id:
                #     is_tempalte_exist = Template.objects.filter(id=template_id).exists()
                #     if is_tempalte_exist:
                #         _s['can_update'] = True
                #         _s['can_update_msg'] = ''

                # 备注中的更新时间比 db 中的更新时间早的话，不允许更新 （watch 上报数据会有延迟）
                if _s['can_update'] and _s['update_time']:
                    if project_kind == 2:
                        # mesos 相关数据
                        s_cate = 'service'
                    else:
                        s_cate = 'K8sService'

                    # 获取db中的更新时间
                    _instance_sets = InstanceConfig.objects.filter(
                        namespace=namespace_id,
                        category=s_cate,
                        name=_s['resourceName'],
                        is_deleted=False
                    )
                    if _instance_sets:
                        is_upateing = _instance_sets.filter(
                            updated__gt=_s['update_time'], oper_type='update').exists()
                        if is_upateing:
                            _s['status'] = 'updating'
                            _s['can_update'] = _s['can_delete'] = False
                            _s['can_update_msg'] = _s['can_delete_msg'] = u"正在更新中，请稍后操作"
            data += cluster_services
        # 按时间倒序排列
        data.sort(key=lambda x: x.get('createTime', ''), reverse=True)

        if data:
            # 检查是否用命名空间的使用权限
            perm = bcs_perm.Namespace(request, project_id, bcs_perm.NO_RES)
            data = perm.hook_perms(data, ns_id_flag='namespace_id', cluster_id_flag='clusterId',
                                   ns_name_flag='namespace')
        return APIResponse({
            "code": ErrorCode.NoError,
            "data": {
                "data": data,
                "length": len(data)
            },
            "message": "ok"
        })

    def check_namespace_use_perm(self, request, project_id, namespace_list):
        """检查是否有命名空间的使用权限
        """
        access_token = request.user.token.access_token

        # 根据 namespace  查询 ns_id
        namespace_res = paas_cc.get_namespace_list(
            access_token, project_id, limit=constants.ALL_LIMIT)
        namespace_data = namespace_res.get('data', {}).get('results') or []
        namespace_dict = {i['name']: i['id'] for i in namespace_data}
        for namespace in namespace_list:
            namespace_id = namespace_dict.get(namespace)
            # 检查是否有命名空间的使用权限
            perm = bcs_perm.Namespace(request, project_id, namespace_id)
            perm.can_use(raise_exception=True)
        return namespace_dict

    def delete_single_service(self, request, project_id, project_kind, cluster_id, namespace, namespace_id, name):
        username = request.user.username
        access_token = request.user.token.access_token

        if project_kind == 2:
            client = mesos.MesosClient(
                access_token, project_id, cluster_id, env=None)
            resp = client.delete_service(namespace, name)
            s_cate = 'service'
        else:
            if namespace in constants.K8S_SYS_NAMESPACE:
                return {
                    "code": 400,
                    "message": u"不允许操作系统命名空间[%s]" % ','.join(constants.K8S_SYS_NAMESPACE),
                }
            client = k8s.K8SClient(
                access_token, project_id, cluster_id, env=None)
            resp = client.delete_service(namespace, name)
            s_cate = 'K8sService'

        if resp.get("code") == ErrorCode.NoError:
            # 删除成功则更新状态
            now_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            InstanceConfig.objects.filter(
                namespace=namespace_id,
                category=s_cate,
                name=name,
            ).update(
                creator=username,
                updator=username,
                oper_type=DELETE_INSTANCE,
                updated=now_time,
                deleted_time=now_time,
                is_deleted=True,
                is_bcs_success=True
            )

        return {
            "code": resp.get("code"),
            "message": resp.get("message"),
        }

    def delete_services(self, request, project_id, cluster_id, namespace, name):
        username = request.user.username
        # 检查用户是否有命名空间的使用权限
        namespace_dict = self.check_namespace_use_perm(request, project_id, [namespace])
        namespace_id = namespace_dict.get(namespace)

        flag, project_kind = self.get_project_kind(request, project_id)
        if not flag:
            return project_kind

        # 删除service
        resp = self.delete_single_service(request, project_id, project_kind, cluster_id, namespace, namespace_id, name)
        # 添加操作审计
        activity_client.ContextActivityLogClient(
            project_id=project_id,
            user=username,
            resource_type="instance",
            resource=name,
            resource_id=0,
            extra=json.dumps({}),
            description=u"删除Service[%s]命名空间[%s]" % (
                name, namespace)
        ).log_modify(activity_status="succeed" if resp.get("code") == ErrorCode.NoError else "failed")

        # 已经删除的，需要将错误信息翻译一下
        message = resp.get('message', '')
        is_delete_before = True if 'node does not exist' in message or 'not found' in message else False
        if is_delete_before:
            message = u"%s[命名空间:%s]已经被删除，请手动刷新数据" % (name, namespace)
        return Response({
            "code": resp.get("code"),
            "message": message,
            "data": {}
        })

    def batch_delete_services(self, request, project_id):
        """批量删除service
        """
        username = request.user.username
        slz = BatchResourceSLZ(data=request.data)
        slz.is_valid(raise_exception=True)
        data = slz.data['data']

        # 检查用户是否有命名空间的使用权限
        namespace_list = [_d.get('namespace') for _d in data]
        namespace_list = set(namespace_list)
        namespace_dict = self.check_namespace_use_perm(request, project_id, namespace_list)

        flag, project_kind = self.get_project_kind(request, project_id)
        if not flag:
            return project_kind

        success_list = []
        failed_list = []
        for _d in data:
            cluster_id = _d.get('cluster_id')
            name = _d.get('name')
            namespace = _d.get('namespace')
            namespace_id = namespace_dict.get(namespace)
            # 删除service
            resp = self.delete_single_service(request, project_id, project_kind,
                                              cluster_id, namespace, namespace_id, name)
            # 处理已经删除，但是storage上报数据延迟的问题
            message = resp.get('message', '')
            is_delete_before = True if 'node does not exist' in message or 'not found' in message else False
            if (resp.get("code") == ErrorCode.NoError):
                success_list.append({
                    'name': name,
                    'desc': u'%s[命名空间:%s]' % (name, namespace),
                })
            else:
                if is_delete_before:
                    message = u'已经被删除，请手动刷新数据'
                failed_list.append({
                    'name': name,
                    'desc': u'%s[命名空间:%s]:%s' % (name, namespace, message),
                })
        code = 0
        message = ''
        # 添加操作审计
        if success_list:
            name_list = [_s.get('name') for _s in success_list]
            desc_list = [_s.get('desc') for _s in success_list]
            message = u"以下service删除成功:%s" % ";".join(desc_list)
            activity_client.ContextActivityLogClient(
                project_id=project_id,
                user=username,
                resource_type="instance",
                resource=';'.join(name_list),
                resource_id=0,
                extra=json.dumps({}),
                description=";".join(desc_list)
            ).log_modify(activity_status="succeed")

        if failed_list:
            name_list = [_s.get('name') for _s in failed_list]
            desc_list = [_s.get('desc') for _s in failed_list]

            code = 4004
            message = u"以下service删除失败:%s" % ";".join(desc_list)
            activity_client.ContextActivityLogClient(
                project_id=project_id,
                user=username,
                resource_type="instance",
                resource=';'.join(name_list),
                resource_id=0,
                extra=json.dumps({}),
                description=message
            ).log_modify(activity_status="failed")

        return Response({
            "code": code,
            "message": message,
            "data": {}
        })

    def update_services(self, request, project_id, cluster_id, namespace, name):
        """更新 service
        """
        access_token = request.user.token.access_token
        flag, project_kind = self.get_project_kind(request, project_id)
        if not flag:
            return project_kind

        if project_kind == 2:
            # mesos 相关数据
            slz_class = ServiceCreateOrUpdateSLZ
            s_sys_con = SEVICE_SYS_CONFIG
            s_cate = 'service'
        else:
            if namespace in constants.K8S_SYS_NAMESPACE:
                return Response({
                    "code": 400,
                    "message": u"不允许操作系统命名空间[%s]" % ','.join(constants.K8S_SYS_NAMESPACE),
                    "data": {}
                })
            # k8s 相关数据
            slz_class = K8sServiceCreateOrUpdateSLZ
            s_sys_con = K8S_SEVICE_SYS_CONFIG
            s_cate = 'K8sService'

        request_data = request.data or {}
        request_data['version_id'] = request_data['version']
        request_data['item_id'] = 0
        request_data['project_id'] = project_id
        show_version_name = request_data.get('show_version_name', '')
        # 验证请求参数
        slz = slz_class(data=request.data)
        slz.is_valid(raise_exception=True)
        data = slz.data
        namespace_id = data['namespace_id']

        # 检查是否有命名空间的使用权限
        perm = bcs_perm.Namespace(request, project_id, namespace_id)
        perm.can_use(raise_exception=True)

        config = json.loads(data['config'])
        #  获取关联的应用列表
        version_id = data['version_id']
        version_entity = VersionedEntity.objects.get(id=version_id)
        entity = version_entity.get_entity()

        # 实例化时后台需要做的处理
        if project_kind == 2:
            app_weight = json.loads(data['app_id'])
            apps = entity.get('application') if entity else None
            application_id_list = apps.split(',') if apps else []

            app_id_list = app_weight.keys()
            service_app_list = Application.objects.filter(
                id__in=application_id_list, app_id__in=app_id_list)

            lb_name = data.get('lb_name', '')
            handel_service_db_config(
                config, service_app_list, app_weight, lb_name, version_id)
        else:
            logger.exception(f"deploy_tag_list {type(data['deploy_tag_list'])}")
            handel_k8s_service_db_config(
                config, data['deploy_tag_list'], version_id, is_upadte=True)
            resource_version = data['resource_version']
            config['metadata']['resourceVersion'] = resource_version
            cluster_version = get_cluster_version(access_token, project_id, cluster_id)
            config = handle_k8s_api_version(config, cluster_id, cluster_version, 'Service')
        # 前端的缓存数据储存到备注中
        config = handle_webcache_config(config)

        # 获取上下文信息
        username = request.user.username
        now_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        context = {
            'SYS_CLUSTER_ID': cluster_id,
            'SYS_NAMESPACE': namespace,
            'SYS_VERSION_ID': version_id,
            'SYS_PROJECT_ID': project_id,
            'SYS_OPERATOR': username,
            'SYS_TEMPLATE_ID': version_entity.template_id,
            'SYS_VERSION': show_version_name,
            'LABLE_VERSION': show_version_name,
            'SYS_INSTANCE_ID': data['instance_id'],
            'SYS_CREATOR': data.get('creator', ''),
            'SYS_CREATE_TIME': data.get('create_time', ''),
            'SYS_UPDATOR': username,
            'SYS_UPDATE_TIME': now_time,
        }
        bcs_context = get_bcs_context(access_token, project_id)
        context.update(bcs_context)

        # 生成配置文件
        sys_config = copy.deepcopy(s_sys_con)
        resource_config = update_nested_dict(config, sys_config)
        resource_config = json.dumps(resource_config)
        try:
            config_profile = render_mako_context(resource_config, context)
        except Exception:
            logger.exception(u"配置文件变量替换出错\nconfig:%s\ncontext:%s" %
                             (resource_config, context))
            raise ValidationError(u"配置文件中有未替换的变量")

        service_name = config.get('metadata', {}).get('name')
        _config_content = {
            'name': service_name,
            'config': json.loads(config_profile),
            'context': context
        }

        # 更新 service
        config_objs = InstanceConfig.objects.filter(
            namespace=namespace_id,
            category=s_cate,
            name=service_name,
        )
        if config_objs.exists():
            config_objs.update(
                creator=username,
                updator=username,
                oper_type='update',
                updated=now_time,
                is_deleted=False,
            )
            _instance_config = config_objs.first()
        else:
            _instance_config = InstanceConfig.objects.create(
                namespace=namespace_id,
                category=s_cate,
                name=service_name,
                config=config_profile,
                instance_id=data.get('instance_id', 0),
                creator=username,
                updator=username,
                oper_type='update',
                updated=now_time,
                is_deleted=False
            )
        _config_content['instance_config_id'] = _instance_config.id
        configuration = {
            namespace_id: {
                s_cate: [_config_content]
            }
        }

        driver = get_scheduler_driver(
            access_token, project_id, configuration)
        result = driver.instantiation(is_update=True)

        failed = []
        if isinstance(result, dict):
            failed = result.get('failed') or []
        # 添加操作审计
        activity_client.ContextActivityLogClient(
            project_id=project_id,
            user=username,
            resource_type="instance",
            resource=service_name,
            resource_id=_instance_config.id,
            extra=json.dumps(configuration),
            description=u"更新Service[%s]命名空间[%s]" % (
                service_name, namespace)
        ).log_modify(activity_status="failed" if failed else "succeed")

        if failed:
            return Response({
                "code": 400,
                "message": "Service[%s]在命名空间[%s]更新失败，请联系集群管理员解决" % (service_name, namespace),
                "data": {}
            })
        return Response({
            "code": 0,
            "message": "OK",
            "data": {
            }
        })


# class Service(viewsets.ViewSet):
#     renderer_classes = (BKAPIRenderer, BrowsableAPIRenderer)
#
#     def list(self, request, project_id):
#         """get service list with pagination
#         """
#         slz = serializers.ServiceListSLZ(request.query_params)
#         slz.is_valid(raise_exception=True)
#         params = slz.validated_data