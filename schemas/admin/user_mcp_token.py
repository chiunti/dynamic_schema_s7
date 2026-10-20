import secrets

from django import forms
from django.contrib import admin, messages
from django.contrib.admin.widgets import AdminDateWidget
from django.utils import timezone
from django.utils.html import format_html

from ..models import UserMCPToken
from ..services.user_mcp_token_service import UserMCPTokenService


class UserMCPTokenAddForm(forms.ModelForm):
    expires_at = forms.DateField(
        widget=AdminDateWidget(),
        help_text="The token stops authenticating the day after this date.",
    )

    class Meta:
        model = UserMCPToken
        fields = ("user", "name", "mcp_level", "expires_at")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["user"].queryset = (
            self.fields["user"].queryset.filter(is_active=True).order_by("email")
        )

    def clean_expires_at(self):
        expires_at = self.cleaned_data["expires_at"]
        if expires_at < timezone.localdate():
            raise forms.ValidationError("Token expiry must not be in the past")
        return expires_at


class UserMCPTokenChangeForm(forms.ModelForm):
    expires_at = forms.DateField(
        widget=AdminDateWidget(),
        help_text="The token stops authenticating the day after this date.",
    )
    is_revoked = forms.BooleanField(
        required=False,
        label="Revoked",
        help_text="Check to revoke this token. Revocation is permanent.",
    )

    class Meta:
        model = UserMCPToken
        fields = ("mcp_level", "expires_at")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            self.fields["is_revoked"].initial = bool(self.instance.revoked_at)
            self.fields["is_revoked"].disabled = bool(self.instance.revoked_at)


@admin.register(UserMCPToken)
class UserMCPTokenAdmin(admin.ModelAdmin):
    list_display = ("name", "user_email", "level", "expires_at", "status", "created_at")
    list_filter = ("mcp_level",)
    search_fields = ("name", "user__email")
    readonly_fields = (
        "user", "name", "token_digest", "revoked_at", "created_at",
    )
    actions = ("revoke_tokens",)

    def get_form(self, request, obj=None, **kwargs):
        if obj is None:
            return UserMCPTokenAddForm
        return UserMCPTokenChangeForm

    def get_fieldsets(self, request, obj=None):
        if obj is None:
            return (
                (None, {
                    "fields": ("user", "name"),
                    "description": "A random secret is generated on save and shown only once.",
                }),
                ("Capability", {
                    "fields": ("mcp_level",),
                    "description": "Level 1: schema authoring. Level 2: catalog authoring.",
                }),
                ("Lifecycle", {
                    "fields": ("expires_at",),
                }),
            )
        return (
            (None, {
                "fields": ("user", "name"),
            }),
            ("Capability", {
                "fields": ("mcp_level",),
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

    @admin.display(description="User")
    def user_email(self, obj):
        return obj.user.email

    @admin.display(description="Level")
    def level(self, obj):
        return obj.get_mcp_level_display()

    @admin.display(description="Status")
    def status(self, obj):
        if obj.revoked_at:
            return "revoked"
        if obj.expires_at < timezone.localdate():
            return "expired"
        return "active"

    @admin.action(description="Revoke selected tokens", permissions=("change",))
    def revoke_tokens(self, request, queryset):
        service = UserMCPTokenService()
        revoked = sum(
            1 for token in queryset
            if service.revoke(token.id)
        )
        self.message_user(request, f"{revoked} token(s) revoked")

    def save_model(self, request, obj, form, change):
        if change:
            super().save_model(request, obj, form, change)
            if form.cleaned_data.get("is_revoked") and not obj.revoked_at:
                UserMCPTokenService().revoke(obj.id)
                self.message_user(request, "Token revoked.", level=messages.WARNING)
            return
        raw = secrets.token_urlsafe(48)
        token = UserMCPTokenService().provision(
            form.cleaned_data["user"],
            form.cleaned_data["name"],
            raw,
            form.cleaned_data["mcp_level"],
            form.cleaned_data["expires_at"],
        )
        obj.pk = token.pk
        self.message_user(
            request,
            format_html(
                "Token created for <b>{}</b>. Copy it now — only its digest is "
                "stored and it will not be shown again: <code>{}</code>",
                token.user.email, raw,
            ),
            level=messages.WARNING,
        )

    def get_queryset(self, request):
        # No organization scoping: tokens are user-bound and the whole admin is
        # superuser-only (see the *_permission hooks below).
        return super().get_queryset(request).select_related("user")

    def has_module_permission(self, request):
        return request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def _is_deletable(self, obj) -> bool:
        # Business rule lives in the service layer (single source of truth).
        return UserMCPTokenService().is_deletable(obj)

    def has_delete_permission(self, request, obj=None):
        if not request.user.is_superuser:
            return False
        return obj is None or self._is_deletable(obj)

    def delete_model(self, request, obj):
        if not self._is_deletable(obj):
            self.message_user(
                request,
                "Only revoked or expired tokens can be deleted. Revoke it first.",
                level=messages.ERROR,
            )
            return
        super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        deletable = [token for token in queryset if self._is_deletable(token)]
        skipped = len(queryset) - len(deletable)
        for token in deletable:
            token.delete()
        self.message_user(request, f"{len(deletable)} token(s) deleted")
        if skipped:
            self.message_user(
                request,
                f"{skipped} active token(s) were not deleted. Revoke them first.",
                level=messages.WARNING,
            )
