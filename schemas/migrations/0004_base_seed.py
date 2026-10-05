import json
from pathlib import Path

from django.core.management import call_command
from django.db import migrations


# Base seed: universal DataTypes only, loaded from the canonical fixture
# schemas/fixtures/base_datatypes.json.
#
# No NodeTypes, NodeTypeCompositions, Domains, or AttributeDefs here — those
# belong to the domain-specific fixtures (example survey, forms, sdui, ...).
# With 0001-0004 applied the database is fully operational for any schema type;
# domain fixtures are additive on top.

FIXTURE_NAME = 'base_datatypes'
FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent / 'fixtures'
    / f'{FIXTURE_NAME}.json'
)


def seed_base_data(apps, schema_editor):
    call_command('loaddata', FIXTURE_NAME, app_label='schemas')


def remove_base_data(apps, schema_editor):
    """Delete the rows owned by the fixture (matched by their fixture PKs)."""
    DataType = apps.get_model('schemas', 'DataType')
    fixture = json.loads(FIXTURE_PATH.read_text(encoding='utf-8'))
    DataType.objects.filter(
        id__in=[o['pk'] for o in fixture if o['model'] == 'schemas.datatype'],
    ).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("schemas", "0003_s7_views"),
    ]

    operations = [
        migrations.RunPython(seed_base_data, reverse_code=remove_base_data),
    ]
