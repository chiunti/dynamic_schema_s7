"""DRF serializers for the schemas app."""

from rest_framework import serializers


class SchemaImportSerializer(serializers.Serializer):
    schema_text = serializers.CharField(
        required=True,
        allow_blank=False,
        trim_whitespace=False,
        help_text="JSON schema as a string.",
    )
    node_type = serializers.CharField(
        required=True,
        allow_blank=False,
        help_text="Root node type name or json_scope (e.g. design_token_collection).",
    )
    project_id = serializers.UUIDField(
        required=False,
        help_text="Optional project UUID; must match the authenticated project.",
    )
    organization_id = serializers.UUIDField(
        required=False,
        allow_null=True,
        help_text="Optional organization UUID.",
    )
    schema_key = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Optional schema key. Falls back to the root JSON 'key' field.",
    )
    schema_version = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Optional schema version. Falls back to the root JSON 'version' field.",
    )
    schema_status = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Optional schema status. Falls back to the root JSON 'status' field or 'draft'.",
    )
    overwrite = serializers.BooleanField(
        required=False,
        default=False,
        help_text="Whether to overwrite an existing schema with the same key and version.",
    )


class CatalogImportSerializer(serializers.Serializer):
    objects = serializers.ListField(
        child=serializers.DictField(),
        allow_empty=False,
        help_text=(
            "Fixture-format catalog objects: each entry is a dict shaped "
            "{model, pk?, fields} where 'model' is a catalog model label "
            "(e.g. nodetype, attributedef, domain), 'pk' an optional UUID, "
            "and 'fields' the object field values."
        ),
    )
