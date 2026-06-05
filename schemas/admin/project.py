from django.contrib import admin
from django.utils.html import format_html

from ..models import Project


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "organization_link", "created_by", "created_at")
    list_filter = ("organization",)
    search_fields = ("name", "slug")
    readonly_fields = ("created_at", "updated_at", "created_by")
    prepopulated_fields = {"slug": ("name",)}
    autocomplete_fields = ("organization",)

    fieldsets = (
        (None, {
            "fields": ("name", "slug", "description", "organization"),
        }),
        ("Metadata", {
            "fields": ("created_by", "created_at", "updated_at"),
            "classes": ("collapse",),
        }),
    )

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)

    @admin.display(description="Organization")
    def organization_link(self, obj):
        return format_html(
            '<span style="font-size:0.85em;color:#666;">{}</span>',
            obj.organization.name,
        )

    def get_queryset(self, request):
        qs = super().get_queryset(request).select_related("organization", "created_by")
        if request.user.is_superuser:
            return qs
        from ..repositories.multi_tenant_repository import MultiTenantRepository
        accessible_org_ids = MultiTenantRepository().get_accessible_organization_ids(request.user)
        return qs.filter(organization_id__in=accessible_org_ids)

    def has_change_permission(self, request, obj=None):
        if request.user.is_superuser:
            return True
        if obj is None:
            return True
        from ..services.permission_service import PermissionService
        return PermissionService().can_edit_project(request.user, obj.id)

    def has_delete_permission(self, request, obj=None):
        if request.user.is_superuser:
            return True
        if obj is None:
            return False
        from ..services.permission_service import PermissionService
        return PermissionService().can_edit_project(request.user, obj.id)
