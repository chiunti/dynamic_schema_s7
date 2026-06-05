from django.db import migrations


# Base seed: Universal DataTypes only.
# No NodeTypes, NodeTypeCompositions, Domains, or AttributeDefs here — those
# belong to the domain-specific seeds (survey example, forms, sdui, ...).
#
# With only 0001-0004 applied the database is fully operational for any
# schema type; domain seeds are additive on top.

DATA_TYPES = [
    ('string',      'Text (stored in value_string)'),
    ('number',      'Decimal number (stored in value_number)'),
    ('bool',        'Boolean (stored in value_bool)'),
    ('json',        'Arbitrary JSONB (stored in value_json)'),
    ('date',        'Date stored as ISO string (stored in value_string)'),
    ('list_string', 'List of strings (stored in value_json array)'),
    # Phase-0 semantic extensions
    ('int',         'Integer (stored in value_number)'),
    ('float',       'Decimal / float (stored in value_number)'),
    ('color',       'CSS / hex color string (stored in value_string)'),
    ('int_tuple',   'Fixed-length array of integers (stored in value_json)'),
    ('dict',        'Arbitrary dictionary (stored in value_json object)'),
    ('list_int',    'List of integers (stored in value_json array)'),
    ('domain_list', 'List of domain item values (stored in value_json array, validated against domain)'),
    # UUID types
    ('uuid',        'UUID string (stored in value_string)'),
    ('auto_uuid',   'Auto-generated UUID (stored in value_string, auto-assigned)'),
    # Natural column mappings — these datatypes map directly to internal schema_nodes columns.
    # No row is created in schema_node_attributes; s7_build_node_json reads the column directly.
    ('natural_uuid',     'Maps to schema_nodes.id (primary key UUID) — read-only in JSON export'),
    ('natural_key',      'Maps to schema_nodes.key column — bidirectional sync with JSON export'),
    ('natural_version',  'Maps to schema_nodes.version column — bidirectional sync with JSON export, enforces key+version uniqueness'),
    ('natural_order',    'Maps to schema_nodes.sort_order column — bidirectional sync with JSON export (0-based)'),
    ('display_order',    'Maps to schema_nodes.sort_order column — bidirectional sync with JSON export (1-based for human readability)'),
]

# No domain-specific seeds here — those belong to each domain seed migration.
# This migration only creates the universal DataTypes.


def seed_base_data(apps, schema_editor):
    DataType = apps.get_model('schemas', 'DataType')

    for name, description in DATA_TYPES:
        DataType.objects.get_or_create(name=name, defaults={'description': description})


def remove_base_data(apps, schema_editor):
    DataType = apps.get_model('schemas', 'DataType')

    DataType.objects.filter(name__in=[dt[0] for dt in DATA_TYPES]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("schemas", "0003_s7_views"),
    ]

    operations = [
        migrations.RunPython(seed_base_data, reverse_code=remove_base_data),
    ]
