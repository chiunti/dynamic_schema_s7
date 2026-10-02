"""
BuildStateAdmin with dynamic tabs for filtering by node type
"""

from django.contrib import admin
from django.http import JsonResponse, HttpResponseRedirect
from django.urls import path, reverse
from django.utils.html import format_html
from django.contrib.admin.views.main import IncorrectLookupParameters

from ..models import BuildState
from ..services.schema_service import SchemaService
from ..repositories.schema_repository import SchemaRepository
from ..repositories.project_repository import ProjectRepository
from ..repositories.node_type_repository import NodeTypeRepository
from ..constants import (
    ERR_NODE_NOT_FOUND,
    ERR_PUBLISH_FAILED,
    ERR_REBUILD_FAILED,
    STATUS_DRAFT,
    STATUS_PUBLISHED,
)


class ProjectListFilter(admin.SimpleListFilter):
    title = 'project'
    parameter_name = 'project'

    def lookups(self, request, model_admin):
        project_repository = ProjectRepository()
        projects = project_repository.get_all_projects_ordered()
        return [(str(p.id), p.name) for p in projects]

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(project_id=self.value())
        return queryset


@admin.register(BuildState)
class BuildStateAdmin(admin.ModelAdmin):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.schema_service = SchemaService()
        self.schema_repository = SchemaRepository()
        self.node_type_repository = NodeTypeRepository()

    change_list_template = "admin/schemas/buildstate/change_list.html"
    list_display = ("key", "schema_type", "project_display", "version", "current_build_display", "last_cached_build_display", "status", "action", "updated_at", "cached_at")
    search_fields = ("key", "version", "project__name")
    list_filter = (ProjectListFilter,)
    preserve_filters = False

    def get_changelist_instance(self, request):
        """Override to capture node_type filter before Django's changelist processes it."""
        self._node_type_filter = request.GET.get('node_type')
        if 'node_type' in request.GET:
            from django.http import QueryDict
            original_get = request.GET
            modified_get = QueryDict(mutable=True)
            modified_get.update(original_get)
            if 'node_type' in modified_get:
                del modified_get['node_type']
            request._original_get = original_get
            request.GET = modified_get
            try:
                cl = super().get_changelist_instance(request)
            finally:
                request.GET = request._original_get
                del request._original_get
            return cl
        return super().get_changelist_instance(request)

    def changelist_view(self, request, extra_context=None):
        """Override to add dynamic node type tabs for root types only"""
        extra_context = extra_context or {}

        node_types = self.node_type_repository.get_node_types_by_scope_endswith('_root')

        tabs = [{'label': 'All', 'value': ''}]
        for nt in node_types:
            label = nt.name.replace('_', ' ').title()
            tabs.append({'label': label, 'value': nt.name})

        extra_context['node_type_tabs'] = tabs
        # Get current_node_type from the original request.GET before it's modified by get_changelist_instance
        current_node_type = request.GET.get('node_type', '')
        extra_context['current_node_type'] = current_node_type
        # Store the node_type for get_changelist_instance to use
        self._node_type_filter = current_node_type

        try:
            return super().changelist_view(request, extra_context=extra_context)
        except IncorrectLookupParameters:
            if 'node_type' in request.GET:
                new_params = request.GET.copy()
                new_params.pop('node_type', None)
                return HttpResponseRedirect(request.path + '?' + new_params.urlencode())
            raise

    def get_queryset(self, request):
        """Filter by node_type if selected in tab"""
        qs = super().get_queryset(request)
        if not request.user.is_superuser:
            from ..repositories.multi_tenant_repository import MultiTenantRepository
            accessible_org_ids = MultiTenantRepository().get_accessible_organization_ids(request.user)
            qs = qs.filter(project__organization_id__in=accessible_org_ids)
        node_type_name = getattr(self, '_node_type_filter', None)

        if node_type_name:
            from django.db.models import Q
            matching = list(self.schema_repository.get_root_nodes_by_node_type_name(node_type_name))
            if matching:
                q = Q()
                for key, version, project_id in matching:
                    q |= Q(key=key, version=version, project_id=project_id)
                qs = qs.filter(q)
            else:
                qs = qs.none()

        return qs

    @admin.display(description="Project")
    def project_display(self, obj):
        if obj.project:
            return format_html(
                '<span title="Org: {}">{}</span>',
                obj.project.organization.name if obj.project.organization else "N/A",
                obj.project.name
            )
        return format_html('<span style="color:#999;">—</span>')

    @admin.display(description=format_html("Current<br>Build"))
    def current_build_display(self, obj):
        return obj.current_build

    @admin.display(description=format_html("Last<br>Cached<br>Build"))
    def last_cached_build_display(self, obj):
        if obj.last_cached_build is not None:
            return obj.last_cached_build
        return format_html('<span style="color:#999;">—</span>')

    @admin.display(description="Type")
    def schema_type(self, obj):
        try:
            node = self.schema_repository.get_root_node_by_key_version(obj.key, obj.version)
            if node:
                scope = node.node_type.json_scope or node.node_type.name
                label = scope.replace('_root', '').replace('_', ' ').title()
                return format_html('<span class="s7-schema-type">{}</span>', label)
        except Exception:
            pass
        return "Unknown"

    @admin.display(description="Status")
    def status(self, obj):
        try:
            node = self.schema_repository.get_root_node_by_key_version(obj.key, obj.version)
            if node:
                ad_status = self.schema_repository.get_attribute_def_by_node_type_key(node.node_type, 'status')
                if ad_status:
                    status_attr = self.schema_repository.get_node_attribute_by_node_attr_def(node, ad_status)
                    if status_attr and status_attr.value_string:
                        status = status_attr.value_string
                        color = "green" if status == STATUS_PUBLISHED else "orange" if status == STATUS_DRAFT else "gray"
                        return format_html('<span style="color: {};">{}</span>', color, status.capitalize())
        except Exception:
            pass
        return "Unknown"

    @admin.display(description="Action")
    def action(self, obj):
        node_status = None
        node_id = None

        try:
            node = self.schema_repository.get_root_node_by_key_version(obj.key, obj.version)
            if node:
                node_id = node.id
                ad_status = self.schema_repository.get_attribute_def_by_node_type_key(node.node_type, 'status')
                if ad_status:
                    status_attr = self.schema_repository.get_node_attribute_by_node_attr_def(node, ad_status)
                    if status_attr:
                        node_status = status_attr.value_string
        except Exception:
            pass

        if node_status == STATUS_DRAFT and obj.last_cached_build is None and node_id:
            # Shared publish gate: same missing-required rule as publish_view
            blockers = self.schema_service.get_publish_blockers(node)
            if blockers:
                warnings = blockers["warnings"]
                return format_html(
                    '<span class="s7-publish-blocked" title="Missing required properties: {} properties across {} nodes">'
                    '<button class="button" disabled style="opacity:0.6;cursor:not-allowed;">Publish</button>'
                    '<span style="color:#c00;font-size:11px;margin-left:5px;">⚠️ {} node(s) incomplete</span>'
                    '</span>',
                    sum(len(w['missing']) for w in warnings),
                    len(warnings),
                    len(warnings)
                )
            url = reverse("admin:schemas_buildstate_publish", kwargs={"node_id": node_id})
            return format_html('<a href="{}" class="button">Publish</a>', url)

        if obj.current_build != obj.last_cached_build and node_status == STATUS_PUBLISHED:
            url = reverse("admin:schemas_buildstate_rebuild_cache", kwargs={"key": obj.key, "version": obj.version})
            return format_html('<a href="{}" class="button">Rebuild Cache</a>', url)

        return "-"

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path("publish/<uuid:node_id>/", self.admin_site.admin_view(self.publish_view), name="schemas_buildstate_publish"),
            path("rebuild-cache/<str:key>/<str:version>/", self.admin_site.admin_view(self.rebuild_cache_view), name="schemas_buildstate_rebuild_cache"),
        ]
        return custom_urls + urls

    def publish_view(self, request, node_id):
        # View = params + response. All publish gating (UUID coercion,
        # not-found, missing-required blockers, root-node rule, publish)
        # is orchestrated by SchemaService.publish_schema_by_id.
        try:
            blockers = self.schema_service.publish_schema_by_id(node_id)
        except LookupError:
            return JsonResponse({"error": ERR_NODE_NOT_FOUND}, status=404)
        except ValueError as e:
            return JsonResponse({"error": str(e)}, status=400)
        except Exception as e:
            return JsonResponse({"error": ERR_PUBLISH_FAILED, "detail": str(e)}, status=500)

        if blockers:
            return JsonResponse(blockers, status=400)

        return HttpResponseRedirect(reverse("admin:schemas_buildstate_changelist"))

    def rebuild_cache_view(self, request, key, version):
        try:
            self.schema_service.build_schema_cached(key, version)
        except Exception as e:
            return JsonResponse({"error": ERR_REBUILD_FAILED, "detail": str(e)}, status=500)

        return HttpResponseRedirect(reverse("admin:schemas_buildstate_changelist"))
