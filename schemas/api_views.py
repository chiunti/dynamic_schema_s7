import uuid

from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View

from .services.organization_service import OrganizationService
from .services.project_service import ProjectService
from .utils import parse_uuid, json_body, org_to_dict, project_to_dict, member_to_dict


@method_decorator(login_required, name="dispatch")
class OrganizationListCreateView(View):
    def get(self, request):
        service = OrganizationService()
        orgs = service.list_organizations(request.user)
        return JsonResponse({"results": [org_to_dict(o) for o in orgs]})

    def post(self, request):
        service = OrganizationService()
        try:
            data = json_body(request)
            org = service.create_organization(
                name=data.get("name", ""),
                slug=data.get("slug") or None,
                description=data.get("description"),
                user=request.user,
            )
            return JsonResponse(org_to_dict(org), status=201)
        except PermissionError as e:
            return JsonResponse({"error": str(e)}, status=403)
        except ValueError as e:
            return JsonResponse({"error": str(e)}, status=400)
        except Exception as e:
            return JsonResponse({"error": str(e)}, status=500)


@method_decorator(login_required, name="dispatch")
class OrganizationDetailView(View):
    def _get_org_id(self, org_id: str) -> uuid.UUID:
        return parse_uuid(org_id, "org_id")

    def get(self, request, org_id):
        try:
            oid = self._get_org_id(org_id)
            org = OrganizationService().get_organization(oid, request.user)
            return JsonResponse(org_to_dict(org))
        except PermissionError as e:
            return JsonResponse({"error": str(e)}, status=403)
        except ValueError as e:
            return JsonResponse({"error": str(e)}, status=404)

    def patch(self, request, org_id):
        try:
            oid = self._get_org_id(org_id)
            data = json_body(request)
            org = OrganizationService().update_organization(oid, request.user, **data)
            return JsonResponse(org_to_dict(org))
        except PermissionError as e:
            return JsonResponse({"error": str(e)}, status=403)
        except ValueError as e:
            return JsonResponse({"error": str(e)}, status=400)

    def put(self, request, org_id):
        return self.patch(request, org_id)


@method_decorator(login_required, name="dispatch")
class OrganizationMemberListView(View):
    def get(self, request, org_id):
        try:
            oid = parse_uuid(org_id, "org_id")
            members = OrganizationService().get_members(oid, request.user)
            return JsonResponse({"results": [member_to_dict(m) for m in members]})
        except PermissionError as e:
            return JsonResponse({"error": str(e)}, status=403)
        except ValueError as e:
            return JsonResponse({"error": str(e)}, status=400)

    def post(self, request, org_id):
        try:
            oid = parse_uuid(org_id, "org_id")
            data = json_body(request)
            member = OrganizationService().add_member(
                organization_id=oid,
                user_email=data.get("user_email", ""),
                role=data.get("role", "viewer"),
                requesting_user=request.user,
            )
            return JsonResponse(member_to_dict(member), status=201)
        except PermissionError as e:
            return JsonResponse({"error": str(e)}, status=403)
        except ValueError as e:
            return JsonResponse({"error": str(e)}, status=400)


@method_decorator(login_required, name="dispatch")
class OrganizationMemberDetailView(View):
    def delete(self, request, org_id, member_id):
        try:
            oid = parse_uuid(org_id, "org_id")
            uid = parse_uuid(member_id, "member_id")
            OrganizationService().remove_member(oid, uid, request.user)
            return JsonResponse({"ok": True})
        except PermissionError as e:
            return JsonResponse({"error": str(e)}, status=403)
        except ValueError as e:
            return JsonResponse({"error": str(e)}, status=404)


@method_decorator(login_required, name="dispatch")
class ProjectListCreateView(View):
    def get(self, request):
        org_id_str = request.GET.get("organization_id")
        org_id = None
        if org_id_str:
            try:
                org_id = parse_uuid(org_id_str, "organization_id")
            except ValueError as e:
                return JsonResponse({"error": str(e)}, status=400)
        projects = ProjectService().list_projects(request.user, organization_id=org_id)
        return JsonResponse({"results": [project_to_dict(p) for p in projects]})

    def post(self, request):
        try:
            data = json_body(request)
            org_id = None
            if data.get("organization_id"):
                org_id = parse_uuid(data["organization_id"], "organization_id")
            project = ProjectService().create_project(
                name=data.get("name", ""),
                description=data.get("description"),
                organization_id=org_id,
                user=request.user,
                slug=data.get("slug"),
            )
            return JsonResponse(project_to_dict(project), status=201)
        except PermissionError as e:
            return JsonResponse({"error": str(e)}, status=403)
        except ValueError as e:
            return JsonResponse({"error": str(e)}, status=400)
        except Exception as e:
            return JsonResponse({"error": str(e)}, status=500)


@method_decorator(login_required, name="dispatch")
class ProjectDetailView(View):
    def get(self, request, project_id):
        try:
            pid = parse_uuid(project_id, "project_id")
            project = ProjectService().get_project(pid, request.user)
            return JsonResponse(project_to_dict(project))
        except PermissionError as e:
            return JsonResponse({"error": str(e)}, status=403)
        except ValueError as e:
            return JsonResponse({"error": str(e)}, status=404)

    def patch(self, request, project_id):
        try:
            pid = parse_uuid(project_id, "project_id")
            data = json_body(request)
            project = ProjectService().update_project(pid, request.user, **data)
            return JsonResponse(project_to_dict(project))
        except PermissionError as e:
            return JsonResponse({"error": str(e)}, status=403)
        except ValueError as e:
            return JsonResponse({"error": str(e)}, status=400)

    def put(self, request, project_id):
        return self.patch(request, project_id)

    def delete(self, request, project_id):
        try:
            pid = parse_uuid(project_id, "project_id")
            ProjectService().delete_project(pid, request.user)
            return JsonResponse({"ok": True})
        except PermissionError as e:
            return JsonResponse({"error": str(e)}, status=403)
        except ValueError as e:
            return JsonResponse({"error": str(e)}, status=400)
