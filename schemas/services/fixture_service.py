"""Fixture inspection and loading for the admin fixture loader.

Fixtures are declarative JSON in ``schemas/fixtures/`` with deterministic
uuid5 PKs. This service lists what is available on disk, reports load status
against the DB, and loads them via ``loaddata`` — the same mechanism used by
the CLI and the ``0004``/``0005`` migration wrappers.
"""

import hashlib
import io
import json
import re
import uuid
from collections import Counter
from pathlib import Path

from django.core.management import call_command

from ..repositories.fixture_repository import FixtureRepository

FIXTURE_NAME_RE = re.compile(r'^[a-z0-9_]+$')

FIXTURES_DIR = Path(__file__).resolve().parent.parent / 'fixtures'

# Models a fixture may carry — every catalog model plus node trees. Anything
# outside this set (auth internals, sessions, etc.) is not seed data.
ALLOWED_MODELS = {
    'datatype', 'domain', 'domainitem', 'nodetype', 'nodetypecomposition',
    'nodetypevariant', 'attributedef', 'organization', 'project', 'node',
    'nodeattribute',
}

# Natural-key FK fields: (model, field) -> the natural-key fields of the
# referenced model, used to verify references resolve inside the fixture.
NATURAL_KEY_FIELDS = {
    'domain': ('domain_name',),
    'domainitem': ('domain', 'value'),
    'datatype': ('name',),
    'nodetype': ('name',),
    'nodetypecomposition': ('parent_type', 'child_type', 'collection_key'),
    'nodetypevariant': ('node_type', 'variant_key', 'discriminator_attr'),
    'attributedef': ('node_type', 'json_key', 'variant_key'),
    'organization': ('slug',),
    'project': ('organization', 'slug'),
}

FK_FIELDS = {
    'domainitem': [('domain', 'domain')],
    'nodetypecomposition': [
        ('parent_type', 'nodetype'), ('child_type', 'nodetype'),
    ],
    'nodetypevariant': [
        ('node_type', 'nodetype'), ('props_node_type', 'nodetype'),
    ],
    'attributedef': [
        ('node_type', 'nodetype'), ('data_type', 'datatype'),
        ('domain', 'domain'),
    ],
    'project': [('organization', 'organization')],
    'node': [
        ('node_type', 'nodetype'), ('project', 'project'),
        ('organization', 'organization'),
    ],
    'nodeattribute': [('attribute_def', 'attributedef')],
}


class FixtureService:
    """Admin-facing operations over the fixture directory."""

    def __init__(self) -> None:
        self.fixture_repository = FixtureRepository()

    def list_fixtures(self) -> list[dict]:
        """Inventory fixture files with per-row load status.

        Status is derived by matching fixture PKs against the DB:
        ``not_loaded`` / ``partial`` / ``loaded``.
        """
        fixtures = []
        records = self.fixture_repository.get_load_records()
        for path in sorted(FIXTURES_DIR.glob('*.json')):
            raw = path.read_bytes()
            objects = json.loads(raw.decode('utf-8'))
            pks_by_model = {}
            for obj in objects:
                label = obj['model'].split('.')[1]
                pks_by_model.setdefault(label, []).append(obj['pk'])

            existing = sum(
                self.fixture_repository.count_existing(label, pks)
                for label, pks in pks_by_model.items()
            )
            total = len(objects)
            if existing == total:
                status = 'loaded'
            elif existing:
                status = 'partial'
            else:
                status = 'not_loaded'

            # Whether the file on disk differs from what was last loaded.
            # A fully-loaded fixture with no record was loaded outside this
            # tool (migration or CLI loaddata) — adopt the current file as
            # the baseline so it reads up-to-date instead of offering a
            # meaningless reload.
            file_hash = hashlib.sha256(raw).hexdigest()
            recorded = records.get(path.stem)
            if status == 'loaded' and recorded is None:
                self.fixture_repository.record_load(
                    path.stem, file_hash, total,
                )
                recorded = file_hash
            changed = recorded is not None and recorded != file_hash

            fixtures.append({
                'name': path.stem,
                'total': total,
                'existing': existing,
                'status': status,
                'file_changed': changed,
                'provenance_known': recorded is not None,
                'by_model': dict(
                    sorted(Counter(
                        o['model'].split('.')[1] for o in objects
                    ).items())
                ),
            })
        return fixtures

    def load_fixture(self, name: str) -> dict:
        """Load one fixture via loaddata. Returns {name, output}."""
        if not FIXTURE_NAME_RE.match(name):
            raise ValueError(f'Invalid fixture name: {name!r}')
        path = FIXTURES_DIR / f'{name}.json'
        if not path.exists():
            raise ValueError(f'Fixture not found: {name}')

        output = io.StringIO()
        call_command(
            'loaddata', str(path), app_label='schemas',
            stdout=output, stderr=output,
        )
        # Provenance: record the loaded file's hash so the loader can
        # distinguish "up to date" from "file changed since load".
        raw = path.read_bytes()
        self.fixture_repository.record_load(
            name, hashlib.sha256(raw).hexdigest(),
            len(json.loads(raw.decode('utf-8'))),
        )
        return {'name': name, 'output': output.getvalue().strip()}

    # ------------------------------------------------------------------ #
    # Upload + validation                                                 #
    # ------------------------------------------------------------------ #

    def save_fixture_file(self, filename: str, content: bytes) -> dict:
        """Validate and store an uploaded fixture under schemas/fixtures/.

        Raises ValueError on any structural problem — the file is only
        written when the whole payload validates.
        """
        stem = filename[:-5] if filename.endswith('.json') else filename
        if not FIXTURE_NAME_RE.match(stem):
            raise ValueError(
                f'Invalid fixture name {stem!r}: use lowercase letters, '
                'digits and underscores',
            )

        try:
            objects = json.loads(content.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ValueError(f'Not valid JSON: {e}') from e

        errors = self.validate_objects(objects)
        if errors:
            raise ValueError(
                'Invalid fixture:\n' + '\n'.join(errors[:10]),
            )

        path = FIXTURES_DIR / f'{stem}.json'
        path.write_text(
            json.dumps(objects, indent=2, ensure_ascii=False) + '\n',
            encoding='utf-8',
        )
        return {'name': stem, 'total': len(objects)}

    def validate_objects(self, objects) -> list[str]:
        """Structural validation of a fixture payload.

        Checks: top-level list, {model,pk,fields} shape, model whitelist,
        uuid PKs, and that every natural-key FK resolves — either to an
        object inside the fixture or to a row already in the database
        (cross-fixture references like base datatypes are legitimate).
        """
        errors = []
        if not isinstance(objects, list) or not objects:
            return ['Fixture must be a non-empty JSON array of objects']

        naturals = {}
        for i, obj in enumerate(objects):
            where = f'object[{i}]'
            if not isinstance(obj, dict):
                errors.append(f'{where}: not an object')
                continue
            model = obj.get('model')
            pk = obj.get('pk')
            fields = obj.get('fields')
            if not isinstance(model, str) or not model.startswith('schemas.'):
                errors.append(f'{where}: missing/invalid model {model!r}')
                continue
            label = model.split('.')[1]
            if label not in ALLOWED_MODELS:
                errors.append(f'{where}: model {model!r} not allowed')
                continue
            try:
                uuid.UUID(str(pk))
            except (ValueError, AttributeError, TypeError):
                errors.append(f'{where}: pk is not a UUID')
            if not isinstance(fields, dict):
                errors.append(f'{where}: missing fields object')
                continue
            nk = self._natural_key_for(label, fields)
            if nk is not None:
                naturals.setdefault(label, set()).add(nk)

        # Second pass: natural-key FK references must resolve in the
        # fixture or in the database.
        for i, obj in enumerate(objects):
            label = obj['model'].split('.')[1]
            fields = obj['fields']
            for field, target_label in FK_FIELDS.get(label, []):
                ref = fields.get(field)
                if ref is None or not isinstance(ref, list):
                    continue
                if tuple(ref) in naturals.get(target_label, set()):
                    continue
                if not self._resolves_in_db(target_label, ref):
                    errors.append(
                        f'object[{i}]: {label}.{field}={ref} does not '
                        f'resolve to a {target_label} row in the fixture '
                        'or the database',
                    )
        return errors

    @staticmethod
    def _resolves_in_db(target_label: str, ref: list) -> bool:
        """Natural-key reference resolves against the live database."""
        from django.apps import apps
        try:
            model = apps.get_model('schemas', target_label)
            model.objects.get_by_natural_key(*ref)
            return True
        except Exception:
            return False

    @staticmethod
    def _natural_key_for(label: str, fields: dict):
        """Rebuild a row's natural key from its serialized fields."""
        def nk1(field):
            ref = fields.get(field)
            return ref[0] if isinstance(ref, list) and ref else ref

        if label == 'domain':
            return (fields.get('domain_name'),)
        if label == 'domainitem':
            return (nk1('domain'), fields.get('value'))
        if label == 'datatype':
            return (fields.get('name'),)
        if label == 'nodetype':
            return (fields.get('name'),)
        if label == 'nodetypecomposition':
            return (
                nk1('parent_type'), nk1('child_type'),
                fields.get('collection_key'),
            )
        if label == 'nodetypevariant':
            return (
                nk1('node_type'), fields.get('variant_key'),
                fields.get('discriminator_attr'),
            )
        if label == 'attributedef':
            return (
                nk1('node_type'), fields.get('json_key'),
                fields.get('variant_key'),
            )
        if label == 'organization':
            return (fields.get('slug'),)
        if label == 'project':
            return (nk1('organization'), fields.get('slug'))
        return None  # node / nodeattribute: pk-referenced, no natural key
