import json
import os
from pathlib import Path

from django.core.management import call_command
from django.db import migrations


# Example seed: the Survey System demo (organization, project, node types,
# domains, and a fully built example survey), loaded from the canonical
# fixture schemas/fixtures/example_survey.json.
#
# Skipped when S7_SKIP_EXAMPLE_SEED is truthy (e.g. production deploys).

FIXTURE_NAME = 'example_survey'
FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent / 'fixtures'
    / f'{FIXTURE_NAME}.json'
)

# Child-to-parent delete order so reverse respects foreign keys.
UNLOAD_ORDER = [
    'nodeattribute', 'node', 'attributedef', 'nodetypevariant',
    'nodetypecomposition', 'nodetype', 'domainitem', 'domain',
    'project', 'organization', 'datatype',
]


def seed_example(apps, schema_editor):
    skip = os.environ.get("S7_SKIP_EXAMPLE_SEED", "").lower()
    if skip in {"1", "true", "yes", "on"}:
        return
    call_command('loaddata', FIXTURE_NAME, app_label='schemas')


def remove_example(apps, schema_editor):
    """Delete the rows owned by the fixture (matched by their fixture PKs)."""
    fixture = json.loads(FIXTURE_PATH.read_text(encoding='utf-8'))
    pks_by_model = {}
    for obj in fixture:
        model_label = obj['model'].split('.')[1]
        pks_by_model.setdefault(model_label, []).append(obj['pk'])

    for model_label in UNLOAD_ORDER:
        pks = pks_by_model.get(model_label)
        if pks:
            model = apps.get_model('schemas', model_label)
            model.objects.filter(id__in=pks).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("schemas", "0004_base_seed"),
    ]

    operations = [
        migrations.RunPython(seed_example, reverse_code=remove_example),
    ]
