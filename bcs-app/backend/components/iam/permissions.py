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
from django.conf import settings

try:
    from iam import IAM, Request, Subject, Action, Resource, OP, MultiActionRequest
    from iam.exceptions import AuthInvalidRequest, AuthAPIError
    from iam.apply import models
    from iam.api.client import Client
    from iam.api.http import http_get, http_post
except Exception:
    pass
from backend.utils.exceptions import PermissionDeniedError
from backend.utils.basic import ChoicesEnum


class IAMClient(Client):
    def _list_policies(self, data):
        path = f"/api/v1/systems/{self._app_code}/policies"
        ok, message, data = self._call_iam_api(http_get, path, data)
        return ok, message, data

    def list_policies(self, data):
        policies = []
        page, page_size = 1, 100
        data["page_size"] = page_size

        while True:
            data["page"] = page
            ok, message, p = self._list_policies(data)
            if not ok:
                raise AuthAPIError(message)
            policies.extend(p["results"])

            left_count = p["count"] - len(p["results"]) - (page - 1) * page_size
            if left_count <= 0:
                return policies

            page += 1

    def list_subjects(self, data):
        path = f"/api/v1/systems/{self._app_code}/policies/-/subjects"
        ok, message, subjects = self._call_iam_api(http_get, path, data)
        if not ok:
            raise AuthAPIError(message)
        return subjects

    def resource_creator_action(self, bk_username, data):
        path = "/api/c/compapi/v2/iam/authorization/resource_creator_action/"

        data.update({"system": self._app_code, "creator": bk_username})

        ok, message, _data = self._call_esb_api(http_post, path, data, None, bk_username, timeout=5)
        if not ok:
            return False, message, ""
        return True, "success", _data.get("data")

    # return resource instance creator to iam, esb needed.
    def grant_resource_creator_actions(self, bk_token, bk_username, data):
        path = "/api/c/compapi/v2/iam/authorization/resource_creator_action/"

        ok, message, _data = self._call_esb_api(http_post, path, data, bk_token, bk_username, timeout=5)
        if not ok:
            return False, message

        return True, "success"


class BCSIAM(IAM):
    def __init__(self, app_code, app_secret, bk_iam_host, bk_paas_host):
        self._client = IAMClient(app_code, app_secret, bk_iam_host, bk_paas_host)

    def make_dict_filter(self, request, key_mapping=None):
        """
        仅支持{'op': 'in', 'field': 'project.id', 'value': [1, 2, 3]}
        或者{'op': 'eq', 'field': 'project.id', 'value': 1}
        """
        # 1. validate
        if not isinstance(request, Request):
            raise AuthInvalidRequest("request should be instance of iam.auth.models.Request")

        request.validate()

        # 2. _client.policy_query
        policies = self._do_policy_query(request)

        # the polices maybe none
        if not policies:
            return None

        op = policies["op"]
        if op not in [OP.IN, OP.EQ, OP.ANY]:
            raise AuthInvalidRequest("make_dict_filter only support OP.IN or OP.EQ or OP.ANY")

        value = policies["value"]
        if op == OP.EQ:
            value = [
                value,
            ]

        field = policies["field"]
        if key_mapping:
            k = key_mapping.get(field) or field
            return {k: value, "op": op}

        return {field: value, "op": op}

    def _match_resource_id(self, expression, resource_type_id, resource_id):
        # 忽略content这一级的op, 暂时仅分析第一个
        content = expression["content"][0]
        if content["field"] != f"{resource_type_id}.id":
            return False

        if content["op"] == OP.IN:
            if resource_id in content["value"]:
                return True
            return False

        if content["op"] == OP.EQ:
            if resource_id == content["value"]:
                return True
            return False

        if content["op"] == OP.ANY:
            return True
        return False

    def query_authorized_users(self, action_id, resource_type_id, resource_id):
        id_list = []
        policies = self._client.list_policies({"action_id": action_id})
        for p in policies:
            if self._match_resource_id(p["expression"], resource_type_id, resource_id):
                id_list.append(str(p["id"]))
        subjects = self._client.list_subjects({"ids": ",".join(id_list)})
        return [{"id": s["subject"]["id"], "name": s["subject"]["name"]} for s in subjects]

    def grant_resource_creator_action(self, bk_username, resource_type_id, resource_id, resource_name):
        data = {
            "type": resource_type_id,
            "id": resource_id,
            "name": resource_name,
            "system": settings.APP_ID,
            "creator": bk_username,
        }
        return self._client.grant_resource_creator_actions(None, bk_username, data)


class Permission:
    iam = BCSIAM(settings.APP_ID, settings.APP_TOKEN, settings.BK_IAM_HOST, settings.BK_PAAS_HOST)
    resource_type_id = None

    def make_application(self, action_id, resource_id):
        if not resource_id:
            action = models.ActionWithoutResources(action_id)
            actions = [action]
            return models.Application(settings.APP_ID, actions)

        instance = models.ResourceInstance([models.ResourceNode(self.resource_type_id, resource_id, resource_id)])
        related_resource_type = models.RelatedResourceType(settings.APP_ID, self.resource_type_id, [instance])
        action = models.ActionWithResources(action_id, [related_resource_type])
        return models.Application(settings.APP_ID, actions=[action])

    def generate_apply_url(self, username, action_id, resource_id=None):
        app = self.make_application(action_id, resource_id)
        ok, message, url = self.iam.get_apply_url(app, bk_username=username)
        if not ok:
            return settings.BK_IAM_APP_URL
        return url

    def _make_request_with_resources(self, username, action_id, resources=None):
        request = Request(settings.APP_ID, Subject("user", username), Action(action_id), resources, None,)
        return request

    def allowed_do_resource_type(self, username, action_id):
        request = self._make_request_with_resources(username, action_id)
        return self.iam.is_allowed(request)

    def resource_type_multi_actions_allowed(self, username, action_ids):
        return {action_id: self.allowed_do_resource_type(username, action_id) for action_id in action_ids}

    def allowed_do_resource_inst(self, username, action_id, resource_type, resource_id, attribute=None):
        attribute = attribute or {}
        r = Resource(settings.APP_ID, resource_type, resource_id, attribute)
        request = self._make_request_with_resources(username, action_id, resources=[r])
        return self.iam.is_allowed(request)

    def resource_inst_multi_actions_allowed(self, username, actions_ids, resource_id):
        resource = Resource(settings.APP_ID, self.resource_type_id, resource_id, {})
        actions = [Action(action_id) for action_id in actions_ids]

        request = MultiActionRequest(settings.APP_ID, Subject("user", username), actions, [resource], None)
        return self.iam.resource_multi_actions_allowed(request)

    def batch_resource_multi_actions_allowed(self, username, actions_ids, resource_ids):
        actions = [Action(action_id) for action_id in actions_ids]
        request = MultiActionRequest(settings.APP_ID, Subject("user", username), actions, [], None)
        resources = []
        for resource_id in resource_ids:
            resources.append([Resource(settings.APP_ID, self.resource_type_id, resource_id, {})])

        return self.iam.batch_resource_multi_actions_allowed(request, resources)


class ProjectActions(ChoicesEnum):
    CREATE = "project_create"
    VIEW = "project_view"
    EDIT = "project_edit"

    _choices_labels = ((CREATE, "project_create"), (VIEW, "project_view"), (EDIT, "project_edit"))


class ProjectPermission(Permission):
    resource_type_id = "project"
    actions = ProjectActions

    def can_create(self, username, raise_exception=False):
        action_id = self.actions.CREATE.value
        is_allowed = self.allowed_do_resource_type(username, action_id)
        if raise_exception and not is_allowed:
            raise PermissionDeniedError(f"no {action_id} permission", self.generate_apply_url(username, action_id))
        return is_allowed

    def _allowed_do_project_inst(self, username, action_id, resource_id, raise_exception=False):
        is_allowed = self.allowed_do_resource_inst(username, action_id, self.resource_type_id, resource_id)
        if raise_exception and not is_allowed:
            raise PermissionDeniedError(
                f"no {action_id} permission", self.generate_apply_url(username, action_id, resource_id)
            )
        return is_allowed

    def can_view(self, username, project_id, raise_exception=False):
        action_id = self.actions.VIEW.value
        return self._allowed_do_project_inst(username, action_id, project_id, raise_exception)

    def can_edit(self, username, project_id, raise_exception=False):
        action_id = self.actions.EDIT.value
        return self._allowed_do_project_inst(username, action_id, project_id, raise_exception)

    def make_view_perm_filter(self, username):
        action_id = self.actions.VIEW.value
        request = self._make_request_with_resources(username, action_id)
        return self.iam.make_dict_filter(request, {"project.id": "project_id_list"})

    def op_is_any(self, filter):
        if not filter:
            return False
        if filter.get("op") == OP.ANY:
            return True
        return False

    def query_user_perms(self, username, **kwargs):
        project_id = kwargs.get("project_id")
        with_apply_url = kwargs.get("with_apply_url")
        action_ids = kwargs.get("action_ids")

        if not project_id:
            perm_allowed = self.resource_type_multi_actions_allowed(username, action_ids)
        else:
            perm_allowed = self.resource_inst_multi_actions_allowed(username, action_ids, project_id)

        user_perms = {action_id: {"is_allowed": perm_allowed[action_id]} for action_id in action_ids}

        if with_apply_url is False:
            return user_perms

        for action_id in action_ids:
            user_perms[action_id]["apply_url"] = self.generate_apply_url(username, action_id, project_id)

        return user_perms

    def query_authorized_users(self, project_id, action_id):
        return self.iam.query_authorized_users(action_id, self.resource_type_id, project_id)

    def grant_related_action_perms(self, username, project_id, project_name):
        return self.iam.grant_resource_creator_action(username, self.resource_type_id, project_id, project_name)
