"""
BuildStateAdmin with dynamic tabs for filtering by node type
"""

from django.contrib import admin
from django.http import JsonResponse, HttpResponseRedirect
from django.urls import path, reverse
from django.utils.html import format_html
from django.contrib.admin.views.main import IncorrectLookupParameters

from ..models import (
    BuildState,
    NodeType,
    AttributeDef,
    Node,
    NodeAttribute,
    Project,
)
from ..services.schema_service import SchemaService
from ..services.schema_validation_service import SchemaValidationService
from ..constants import (
    ERR_INVALID_NODE_ID,
    ERR_NODE_NOT_FOUND,
    ERR_INCOMPLETE_SCHEMA,
    ERR_NOT_A_ROOT_NODE,
    ERR_PUBLISH_FAILED,
    ERR_REBUILD_FAILED,
)


class ProjectListFilter(admin.SimpleListFilter):
    title = 'project'
    parameter_name = 'project'

    def lookups(self, request, model_admin):
        projects = Project.objects.all().order_by('name')
        return [(str(p.id), p.name) for p in projects]

    def queryset(self, request, queryset):
        if self.value():
            return queryset.filter(project_id=self.value())
        return queryset


@admin.register(BuildState)
class BuildStateAdmin(admin.ModelAdmin):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.validation_service = SchemaValidationService()
        self.schema_service = SchemaService()

    change_list_template = "admin/schemas/buildstate/change_list.html"
    list_display = ("key", "schema_type", "project_display", "version", "current_build_display", "last_cached_build_display", "status", "action", "updated_at", "cached_at")
    search_fields = ("key", "version", "project__name")
    list_filter = (ProjectListFilter,)
    preserve_filters = False

    def get_changelist_instance(self, request):
        """Override to capture node_type filter before Django's changelist processes it."""
        self._node_type_filter = request.GET.get('node_type') or ''
        return super().get_changelist_instance(request)

    def changelist_view(self, request, extra_context=None):
        """Override to add dynamic node type tabs for root types only"""
        extra_context = extra_context or {}
        
        node_types = NodeType.objects.filter(json_scope__endswith='_root')
        
        tabs = [{'label': 'All', 'value': ''}]
        for nt in node_types:
            label = nt.name.replace('_', ' ').title()
            tabs.append({'label': label, 'value': nt.name})
        
        extra_context['node_type_tabs'] = tabs
        extra_context['current_node_type'] = request.GET.get('node_type', '')
        
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
        node_type_name = getattr(self, '_node_type_filter', None)
        
        if node_type_name:
            from django.db.models import Q
            matching = list(Node.objects.filter(
                node_type__name=node_type_name,
                parent__isnull=True,
                key__isnull=False,
                version__isnull=False,
            ).values_list('key', 'version'))
            if matching:
                q = Q()
                for k, v in matching:
                    q |= Q(key=k, version=v)
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
            node = Node.objects.filter(
                node_type__is_root=True,
                parent__isnull=True,
                key=obj.key,
                version=obj.version,
            ).select_related('node_type').first()
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
            node = Node.objects.filter(
                node_type__is_root=True,
                parent__isnull=True,
                key=obj.key,
                version=obj.version,
            ).select_related('node_type').first()
            if node:
                ad_status = AttributeDef.objects.filter(node_type=node.node_type, json_key='status').first()
                if ad_status:
                    status_attr = NodeAttribute.objects.filter(node=node, attribute_def=ad_status).first()
                    if status_attr and status_attr.value_string:
                        status = status_attr.value_string
                        color = "green" if status == "published" else "orange" if status == "draft" else "gray"
                        return format_html('<span style="color: {};">{}</span>', color, status.capitalize())
        except Exception:
            pass
        return "Unknown"

    @admin.display(description="Action")
    def action(self, obj):
        node_status = None
        node_id = None
        
        try:
            node = Node.objects.filter(
                node_type__is_root=True,
                parent__isnull=True,
                key=obj.key,
                version=obj.version,
            ).select_related('node_type').first()
            if node:
                node_id = node.id
                ad_status = AttributeDef.objects.filter(node_type=node.node_type, json_key='status').first()
                if ad_status:
                    status_attr = NodeAttribute.objects.filter(node=node, attribute_def=ad_status).first()
                    if status_attr:
                        node_status = status_attr.value_string
        except Exception:
            pass

        if node_status == 'draft' and obj.last_cached_build is None and node_id:
            # Check for missing required properties before allowing publish
            warnings = self._collect_required_warnings(node_id)
            if warnings:
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

        if obj.current_build != obj.last_cached_build and node_status == 'published':
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

    def _collect_required_warnings(self, root_node_id):
        """Walk the node tree and return missing required AttributeDefs per node."""
        return self.validation_service.collect_required_warnings(root_node_id)

    def publish_view(self, request, node_id):
        from uuid import UUID
        try:
            node_id = UUID(node_id) if isinstance(node_id, str) else node_id
            node = Node.objects.filter(id=node_id).select_related('node_type').first()
        except Exception:
            return JsonResponse({"error": ERR_INVALID_NODE_ID}, status=400)
        
        if not node:
            return JsonResponse({"error": ERR_NODE_NOT_FOUND}, status=404)
        
        # Validate no missing required properties before publishing
        warnings = self._collect_required_warnings(node.id)
        if warnings:
            warning_details = [
                f"{w['node_name']} ({w['node_type']}): missing {', '.join(w['missing'])}"
                for w in warnings
            ]
            return JsonResponse({
                "error": ERR_INCOMPLETE_SCHEMA,
                "detail": f"Cannot publish: {len(warnings)} node(s) have missing required properties",
                "warnings": warnings,
                "message": "Complete all required properties before publishing. " + "; ".join(warning_details)
            }, status=400)
        
        if not node.node_type or not node.node_type.is_root:
            return JsonResponse({"error": ERR_NOT_A_ROOT_NODE}, status=400)

        try:
            self.schema_service.publish_schema(node)
        except ValueError as e:
            return JsonResponse({"error": str(e)}, status=400)
        except Exception as e:
            return JsonResponse({"error": ERR_PUBLISH_FAILED, "detail": str(e)}, status=500)

        return HttpResponseRedirect(reverse("admin:schemas_buildstate_changelist"))

    def rebuild_cache_view(self, request, key, version):
        try:
            self.schema_service.build_schema_cached(key, version)
        except Exception as e:
            return JsonResponse({"error": ERR_REBUILD_FAILED, "detail": str(e)}, status=500)

        return HttpResponseRedirect(reverse("admin:schemas_buildstate_changelist"))
