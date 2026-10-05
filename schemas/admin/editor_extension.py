"""Admin dashboard for node-editor extensions.

Registry rows with source='static' mirror files present on disk (auto-created
by the manifest endpoint); source='db' rows are uploaded here and served from
the database, so private extensions need no shipped files.
"""

from django import forms
from django.contrib import admin, messages

from ..models import EditorExtension
from ..services.editor_extension_service import (
    EXTENSION_NAME_RE,
    EditorExtensionService,
)


class EditorExtensionForm(forms.ModelForm):
    upload = forms.FileField(
        required=False,
        label='Extension file',
        help_text=(
            'Upload a .js file — name is taken from an optional '
            '@s7-editor tag in the source header, else from the filename.'
        ),
    )

    class Meta:
        model = EditorExtension
        fields = (
            'name', 'filename', 'source', 'description', 'is_enabled',
            'content', 'upload',
        )
        widgets = {
            'content': forms.Textarea(attrs={'rows': 18, 'cols': 100}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Everything is derivable from the uploaded file (or editable code).
        for field in ('name', 'filename', 'content'):
            self.fields[field].required = False

    def clean_name(self):
        name = self.cleaned_data['name'].strip()
        # 'upload' is cleaned after 'name' — read it from self.files.
        upload = self.files.get('upload')
        if not name and upload is not None:
            raw = upload.read()
            upload.seek(0)
            name = EditorExtensionService.resolve_name(
                upload.name, raw.decode('utf-8', errors='replace'),
            )
        if not EXTENSION_NAME_RE.match(name):
            raise forms.ValidationError(
                'Use lowercase letters, digits and underscores only.'
            )
        # Uploading over an existing row (e.g. a static file being converted
        # to a DB-sourced extension) upserts it instead of failing unique.
        # validate_unique only excludes the current pk when the instance is
        # not in "adding" state, so both flags must be set.
        if upload is not None:
            existing = EditorExtension.objects.filter(name=name).first()
            if existing is not None:
                self.instance.pk = existing.pk
                self.instance._state.adding = False
        return name

    def clean(self):
        cleaned = super().clean()
        upload = cleaned.get('upload')
        content = (cleaned.get('content') or '').strip()
        # Static rows serve from disk — content is readonly and empty;
        # only DB rows / new uploads must provide source.
        is_static = (
            self.instance.pk is not None
            and self.instance.source == EditorExtension.SOURCE_STATIC
        )
        if upload is None and not is_static and not content:
            raise forms.ValidationError(
                'Provide a .js file or paste the extension source.'
            )
        return cleaned


@admin.register(EditorExtension)
class EditorExtensionAdmin(admin.ModelAdmin):
    form = EditorExtensionForm

    list_display = (
        'name', 'filename', 'source', 'is_enabled_badge', 'updated_at',
    )
    list_filter = ('source', 'is_enabled')
    search_fields = ('name', 'filename', 'description')
    ordering = ('name',)
    actions = ('enable_extensions', 'disable_extensions')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.extension_service = EditorExtensionService()

    def changelist_view(self, request, extra_context=None):
        # Reconcile disk files so the dashboard reflects reality, not
        # only the state after the editor's first manifest request.
        self.extension_service.sync_static()
        return super().changelist_view(request, extra_context)

    def get_readonly_fields(self, request, obj=None):
        # 'source' is derived (upload/db vs disk-file sync) — never edited.
        readonly = ['source']
        if obj is not None and obj.source == EditorExtension.SOURCE_STATIC:
            readonly += ['filename', 'content']
        return readonly

    def save_model(self, request, obj, form, change):
        upload = form.cleaned_data.get('upload')
        service = self.extension_service
        if upload is not None:
            try:
                uploaded = service.upload(
                    upload.name,
                    upload.read().decode('utf-8'),
                    description=form.cleaned_data.get('description') or '',
                )
            except (ValueError, UnicodeDecodeError) as e:
                messages.error(request, f'Extension not saved: {e}')
                return
            # The service already persisted the row — only point the form
            # object at it so the admin redirect/log use the right pk.
            # Calling obj.save() here would INSERT with a duplicate pk.
            obj.pk = uploaded.pk
            obj._state.adding = False
            for warning in service.last_warnings:
                messages.warning(request, warning)
            return
        if obj.source == EditorExtension.SOURCE_DB:
            errors, warnings = service.validate_source(obj.content or '')
            if errors:
                messages.error(
                    request, 'Extension not saved: ' + '; '.join(errors),
                )
                return
            for warning in warnings:
                messages.warning(request, warning)
        else:
            obj.content = None
        if not obj.filename:
            obj.filename = f'{obj.name}.js'
        super().save_model(request, obj, form, change)

    @admin.display(description='Enabled', boolean=True)
    def is_enabled_badge(self, obj):
        return obj.is_enabled

    @admin.action(description='Enable selected extensions')
    def enable_extensions(self, request, queryset):
        updated = queryset.update(is_enabled=True)
        self.message_user(
            request, f'{updated} extension(s) enabled.', messages.SUCCESS,
        )

    @admin.action(description='Disable selected extensions')
    def disable_extensions(self, request, queryset):
        updated = queryset.update(is_enabled=False)
        self.message_user(
            request, f'{updated} extension(s) disabled.', messages.WARNING,
        )
