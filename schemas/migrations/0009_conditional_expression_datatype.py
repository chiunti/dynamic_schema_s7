import uuid

from django.db import migrations


# Conditional feature batch — expression datatype seed (feature tag: conditional).
# feature: conditional
#
# Adds the `expression` DataType to the base catalog for databases that
# already applied 0004_base_seed before the row existed in
# fixtures/base_datatypes.json. Fresh installs load it through the fixture;
# this get_or_create by natural key (name) makes the seed idempotent on both
# paths — a row that already exists under a different pk is reused, never
# duplicated.
#
# The datatype stores client-side {{expr}} expression strings verbatim in
# value_string; evaluation happens in the consumer, not in this engine.


EXPRESSION_DATA_TYPE_PK = uuid.UUID('49abbbec-2677-56bb-877d-32e28f41813e')


def add_expression_data_type(apps, schema_editor):
    """Seed the `expression` DataType unless the base fixture already did."""
    DataType = apps.get_model('schemas', 'DataType')
    DataType.objects.get_or_create(
        name='expression',
        defaults={
            'id': EXPRESSION_DATA_TYPE_PK,
            'description': (
                'Client-side {{expr}} expression string '
                '(stored in value_string)'
            ),
            'primary_storage_type': 'string',
            'editor_extension': '',
        },
    )


def remove_expression_data_type(apps, schema_editor):
    """Reverse of add_expression_data_type.

    Deletes only the canonical seeded row (matched by its fixture pk): a
    pre-existing `expression` row under a different pk was not created by
    this migration — or by the fixture — and is left untouched.
    """
    DataType = apps.get_model('schemas', 'DataType')
    DataType.objects.filter(pk=EXPRESSION_DATA_TYPE_PK).delete()


class Migration(migrations.Migration):
    dependencies = [
        ('schemas', '0008_mcp_root_key_version_scope'),
    ]

    operations = [
        migrations.RunPython(
            add_expression_data_type,
            reverse_code=remove_expression_data_type,
        ),
    ]
