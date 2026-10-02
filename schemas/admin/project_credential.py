import secrets

from django import forms
from django.contrib import admin, messages
from django.contrib.admin.widgets import AdminDateWidget
from django.utils import timezone
from django.utils.html import format_html

from ..models import ProjectAPICredential
from ..services.project_credential_service import ProjectCredentialService


class ProjectAPICredentialAddForm(forms.ModelForm):
    expires_at = forms.DateField(
        widget=AdminDateWidget(),
        help_text="The credential stops authenticating the day after this date.",
    )

    class Meta:
        model = ProjectAPICredential
        fields = ("project", "name", "can_read", "can_import", "can_publish", "expires_at")

    def clean_project(self):
        project = self.cleaned_data["project"]
        if not project.organization.is_active:
            raise forms.ValidationError("The project's organization is not active")
        return project

    def clean(self):
        cleaned = super().clean()
        if not any(cleaned.get(flag) for flag in ("can_read", "can_import", "can_publish")):
            raise forms.ValidationError("Grant at least one scope")
        return cleaned

    def clean_expires_at(self):
        expires_at = self.cleaned_data["expires_at"]
        if expires_at < timezone.localdate():
            raise forms.ValidationError("Credential expiry must not be in the past")
        return expires_at


class ProjectAPICredentialChangeForm(forms.ModelForm):
    expires_at = forms.DateField(
        widget=AdminDateWidget(),
        help_text="The credential stops authenticating the day after this date.",
    )
    is_revoked = forms.BooleanField(
        required=False,
        label="Revoked",
        help_text="Check to revoke this credential. Revocation is permanent.",
    )

    class Meta:
        model = ProjectAPICredential
        fields = ("can_read", "can_import", "can_publish", "expires_at")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields["is_revoked"].initial = bool(self.instance.revoked_at)
            self.fields["is_revoked"].disabled = bool(self.instance.revoked_at)

    def clean(self):
        cleaned = super().clean()
        if not any(cleaned.get(flag) for flag in ("can_read", "can_import", "can_publish")):
            raise forms.ValidationError("Grant at least one scope")
        return cleaned


@admin.register(ProjectAPICredential)
class ProjectAPICredentialAdmin(admin.ModelAdmin):
    list_display = ("name", "project_name", "scopes", "expires_at", "status", "created_at")
    list_filter = ("project__organization", "can_read", "can_import", "can_publish")
    search_fields = ("name", "project__name")
    readonly_fields = (
        "project", "name", "token_digest", "revoked_at", "created_at",
    )
    actions = ("revoke_credentials",)

    def get_form(self, request, obj=None, **kwargs):
        if obj is None:
            return ProjectAPICredentialAddForm
        return ProjectAPICredentialChangeForm

    def get_fieldsets(self, request, obj=None):
        if obj is None:
            return (
                (None, {
                    "fields": ("project", "name"),
                    "description": "A random secret is generated on save and shown only once.",
                }),
                ("Scopes", {
                    "fields": ("can_read", "can_import", "can_publish"),
                }),
                ("Lifecycle", {
                    "fields": ("expires_at",),
                }),
            )
        return (
            (None, {
                "fields": ("project", "name"),
            }),
            ("Scopes", {
                "fields": ("can_read", "can_import", "can_publish"),
            }),
            ("Lifecycle", {
                "fields": ("expires_at", "is_revoked", "revoked_at", "created_at"),
            }),
            ("Digest", {
                "fields": ("token_digest",),
                "classes": ("collapse",),
            }),
        )

    def get_readonly_fields(self, request, obj=None):
        if obj is None:
            return ()
        return self.readonly_fields

    @admin.display(description="Project")
    def project_name(self, obj):
        return obj.project.name

    @admin.display(description="Scopes")
    def scopes(self, obj):
        granted = [label for label, flag in (
            ("read", obj.can_read), ("import", obj.can_import), ("publish", obj.can_publish)
        ) if flag]
        return ", ".join(granted) or "—"

    @admin.display(description="Status")
    def status(self, obj):
        if obj.revoked_at:
            return "revoked"
        if obj.expires_at < timezone.localdate():
            return "expired"
        return "active"

    @admin.action(description="Revoke selected credentials", permissions=("change",))
    def revoke_credentials(self, request, queryset):
        service = ProjectCredentialService()
        revoked = sum(
            1 for credential in queryset
            if service.revoke(credential.project_id, credential.id)
        )
        self.message_user(request, f"{revoked} credential(s) revoked")

    def save_model(self, request, obj, form, change):
        if change:
            super().save_model(request, obj, form, change)
            if form.cleaned_data.get("is_revoked") and not obj.revoked_at:
                ProjectCredentialService().revoke(obj.project_id, obj.id)
                self.message_user(request, "Credential revoked.", level=messages.WARNING)
            return
        raw = secrets.token_urlsafe(48)
        credential = ProjectCredentialService().provision(
            form.cleaned_data["project"].id,
            form.cleaned_data["name"],
            raw,
            {label for label, flag in (
                ("read", form.cleaned_data["can_read"]),
                ("import", form.cleaned_data["can_import"]),
                ("publish", form.cleaned_data["can_publish"]),
            ) if flag},
            form.cleaned_data["expires_at"],
        )
        obj.pk = credential.pk
        self.message_user(
            request,
            format_html(
                "Credential created for <b>{}</b>. Copy it now — only its digest is "
                "stored and it will not be shown again: <code>{}</code>",
                credential.name, raw,
            ),
            level=messages.WARNING,
        )

    def get_queryset(self, request):
        qs = super().get_queryset(request).select_related("project", "project__organization")
        if request.user.is_superuser:
            return qs
        from ..repositories.multi_tenant_repository import MultiTenantRepository
        accessible_org_ids = MultiTenantRepository().get_accessible_organization_ids(request.user)
        return qs.filter(project__organization_id__in=accessible_org_ids)

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def _is_deletable(self, obj) -> bool:
        # Business rule lives in the service layer (single source of truth).
        return ProjectCredentialService().is_deletable(obj)

    def has_delete_permission(self, request, obj=None):
        if not request.user.is_superuser:
            return False
        return obj is None or self._is_deletable(obj)

    def delete_model(self, request, obj):
        if not self._is_deletable(obj):
            self.message_user(
                request,
                "Only revoked or expired credentials can be deleted. Revoke it first.",
                level=messages.ERROR,
            )
            return
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        deletable = [credential for credential in queryset if self._is_deletable(credential)]
        skipped = len(queryset) - len(deletable)
        for credential in deletable:
            credential.delete()
        self.message_user(request, f"{len(deletable)} credential(s) deleted")
        if skipped:
            self.message_user(
                request,
                f"{skipped} active credential(s) were not deleted. Revoke them first.",
                level=messages.WARNING,
            )
