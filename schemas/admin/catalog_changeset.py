import json

from django.contrib import admin
from django.utils.html import format_html

from ..models import CatalogChangeSet


@admin.register(CatalogChangeSet)
class CatalogChangeSetAdmin(admin.ModelAdmin):
    """Read-only audit listing for catalog changesets.

    Changesets are created through the governed propose flow (API/service),
    never by hand, and the review/diff/approve/apply UI is delivered in a
    follow-up ticket — so this admin exposes no add, delete, approve or
    apply paths.
    """

    list_display = ("short_id", "status", "proposer", "created_at", "applied_at")
    list_filter = ("status",)
    ordering = ("-created_at",)
    search_fields = (
        "id",
        "rationale",
        "base_revision",
        "proposed_by_credential__name",
        "proposed_by_user__email",
    )
    readonly_fields = (
        "id",
        "status",
        "proposed_by_credential",
        "proposed_by_user",
        "rationale",
        "ops",
        "ops_pretty",
        "scopes",
        "base_revision",
        "validation_report",
        "report_pretty",
        "reviewed_by",
        "applied_at",
        "created_at",
        "updated_at",
    )

    def get_fieldsets(self, request, obj=None):
        if obj is None:
            # The add view is unreachable (has_add_permission -> False); this
            # minimal layout only keeps the hook well-formed.
            return (
                (None, {
                    "fields": ("status", "rationale", "ops"),
                }),
            )
        return (
            ("Identity", {
                "fields": ("id", "status"),
            }),
            ("Proposal", {
                "fields": (
                    "proposed_by_credential",
                    "proposed_by_user",
                    "rationale",
                    "ops_pretty",
                    "scopes",
                ),
            }),
            ("Validation", {
                "fields": ("base_revision", "report_pretty"),
            }),
            ("Review", {
                "fields": (
                    "reviewed_by",
                    "applied_at",
                    "created_at",
                    "updated_at",
                ),
            }),
        )

    def get_readonly_fields(self, request, obj=None):
        if obj is None:
            return ()
        return self.readonly_fields

    @admin.display(description="ID", ordering="id")
    def short_id(self, obj):
        return str(obj.id)[:8]

    @admin.display(description="Proposer")
    def proposer(self, obj):
        if obj.proposed_by_credential_id:
            return obj.proposed_by_credential.name
        if obj.proposed_by_user_id:
            return obj.proposed_by_user.email
        return "—"

    @admin.display(description="Ops")
    def ops_pretty(self, obj):
        if obj.ops is None:
            return "—"
        return format_html("<pre>{}</pre>", json.dumps(obj.ops, indent=2, default=str))

    @admin.display(description="Validation report")
    def report_pretty(self, obj):
        if obj.validation_report is None:
            return "—"
        return format_html(
            "<pre>{}</pre>", json.dumps(obj.validation_report, indent=2, default=str)
        )

    def get_queryset(self, request):
        # Audit listing without tenant scoping: the whole admin is
        # superuser-only (see the *_permission hooks below). select_related
        # keeps the proposer and reviewer columns cheap.
        return super().get_queryset(request).select_related(
            "proposed_by_credential",
            "proposed_by_user",
            "reviewed_by",
        )

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        # Read-only audit object: denying change permission makes Django
        # serve the detail screen without a submit row — otherwise a
        # "Save" would bump ``updated_at`` on a no-op write. Mutation
        # paths (validate/approve/apply) belong to the service layer.
        return False

    def has_add_permission(self, request):
        # Changesets are born through the propose flow, never by hand.
        return False

    def has_delete_permission(self, request, obj=None):
        # Changesets are an audit trail: they are never deleted.
        return False
