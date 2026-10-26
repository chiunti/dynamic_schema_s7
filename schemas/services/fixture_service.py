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
from typing import Optional

from django.core.management import call_command

from ..constants import (
    ERR_INVALID_FK_SHAPE,
    ERR_MISSING_NATURAL_KEY_FIELDS,
    ERR_MISSING_REQUIRED_FIELDS,
    ERR_UNKNOWN_OBJECT_KEYS,
)
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

# Natural-key field positions per model: label -> the ordered field names
# that form the row's natural key. FK positions carry natural-key arrays
# in fixture format (see FK_FIELDS below).
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

    def validate_objects(self, objects, allowed_models=None) -> list[str]:
        """Structural validation of a fixture payload.

        Checks: top-level list, a strict ``{model, pk, fields}``
        allowlist on each object's wrapper keys (any other key — a
        ``delete`` hint, an op verb, a typo — is a per-object error,
        never silently ignored), {model,pk,fields} shape, model whitelist,
        uuid PKs, field names against the target model, required-field
        presence (every non-PK model field that is not ``null=True``
        and has no default must carry a key in ``fields`` — an explicit
        ``null`` counts as absent; natural-key positions are covered by
        the NK check and are not reported twice), natural key
        presence (every position of the model's natural key must be
        present and non-empty — explicit NULL is accepted only on
        nullable model fields), in-payload natural key uniqueness, the
        shape of every declared FK value (``FK_FIELDS`` positions must
        serialize as natural-key lists of strings — ``None`` is the
        valid "no reference" shape; scalars and lists with non-string
        elements are rejected before resolution), and that every
        natural-key FK resolves — either to an object inside the
        fixture or to a row already in the database (cross-fixture
        references like base datatypes are legitimate).

        ``allowed_models`` narrows the label whitelist when the caller
        accepts only a subset (e.g. catalog-only payloads); the default
        is the module-level ``ALLOWED_MODELS``.

        Never raises on arbitrary input: objects that fail the shape
        checks accumulate in ``errors[]`` and are skipped by the
        reference-resolution pass.
        """
        if allowed_models is None:
            allowed_models = ALLOWED_MODELS

        errors = []
        if not isinstance(objects, list) or not objects:
            return ['Fixture must be a non-empty JSON array of objects']

        naturals = {}
        checked = []
        for i, obj in enumerate(objects):
            where = f'object[{i}]'
            if not isinstance(obj, dict):
                errors.append(f'{where}: not an object')
                continue
            # The wrapper is an allowlist: anything outside {model, pk,
            # fields} (a "delete": true hint, an op verb, a typo) is an
            # explicit per-object error — never silently ignored.
            extra = set(obj) - {'model', 'pk', 'fields'}
            if extra:
                errors.append(
                    f'{where}: '
                    + ERR_UNKNOWN_OBJECT_KEYS.format(
                        keys=', '.join(sorted(str(key) for key in extra)),
                    )
                )
                continue
            model = obj.get('model')
            pk = obj.get('pk')
            fields = obj.get('fields')
            if (not isinstance(model, str)
                    or not model.startswith('schemas.')
                    or model.count('.') != 1):
                errors.append(f'{where}: missing/invalid model {model!r}')
                continue
            label = model.split('.')[1]
            if label not in allowed_models:
                errors.append(f'{where}: model {model!r} not allowed')
                continue
            try:
                uuid.UUID(str(pk))
            except (ValueError, AttributeError, TypeError):
                errors.append(f'{where}: pk is not a UUID')
            if not isinstance(fields, dict):
                errors.append(f'{where}: missing fields object')
                continue
            # A serialized 'id' inside fields would override the top-level
            # pk at deserialization time — the pk position is the only
            # sanctioned way to name a row.
            if 'id' in fields:
                errors.append(
                    f"{where}: field 'id' belongs at the top-level 'pk', "
                    'not inside fields',
                )
            field_names = self.fixture_repository.get_model_field_names(label)
            if field_names is not None:
                for name in fields:
                    if name not in field_names:
                        errors.append(
                            f'{where}: unknown field {name!r} '
                            f"for model '{label}'",
                        )
            checked.append((i, label, fields))
            missing_required = self._missing_required_fields(label, fields)
            if missing_required:
                # Non-NK required fields (a CharField that would
                # silently deserialize to '', a non-nullable FK). No
                # continue: the object may also carry an NK error and
                # both entries must accumulate.
                errors.append(
                    f'{where}: '
                    + ERR_MISSING_REQUIRED_FIELDS.format(
                        fields=', '.join(sorted(missing_required)),
                    )
                )
            nk = self.natural_key_for(label, fields)
            if nk is not None:
                missing = self._missing_natural_key_fields(
                    label, fields, nk,
                )
                if missing:
                    # The catalog is natural-keyed: an object without a
                    # complete NK has no identity — it would insert an
                    # anonymous row or match-update a stray one. Reject
                    # instead of deduplicating: an incomplete NK must not
                    # participate in the naturals set either.
                    errors.append(
                        f'{where}: '
                        + ERR_MISSING_NATURAL_KEY_FIELDS.format(
                            fields=', '.join(missing),
                        )
                    )
                    continue
                try:
                    label_naturals = naturals.setdefault(label, set())
                    if nk in label_naturals:
                        # Two payload objects carrying the same natural key
                        # with different pks would both insert when a key
                        # component is NULL (unique constraints treat NULLs
                        # as distinct) — flag instead of silently
                        # duplicating.
                        errors.append(
                            f'{where}: duplicate natural key in payload',
                        )
                    else:
                        label_naturals.add(nk)
                except TypeError:
                    errors.append(f'{where}: malformed natural key')

        # Second pass: declared FK values must carry the natural-key
        # shape, and the references must resolve in the fixture or in
        # the database. Only objects that passed the shape checks above
        # are inspected — malformed ones already carry an errors[] entry
        # and must not crash this pass.
        for i, label, fields in checked:
            for field, target_label in FK_FIELDS.get(label, []):
                ref = fields.get(field)
                if ref is None:
                    # NULL is the valid "no reference" shape.
                    continue
                if (not isinstance(ref, list)
                        or not all(isinstance(v, str) for v in ref)):
                    # A declared FK must serialize as a natural-key list
                    # of strings. Anything else would pass validation
                    # silently and blow up inside the ORM's pk coercion
                    # at apply time with an uncontextualized conversion
                    # error.
                    received = (
                        'a scalar'
                        if not isinstance(ref, list)
                        else 'a list with non-string elements'
                    )
                    errors.append(
                        f'object[{i}]: '
                        + ERR_INVALID_FK_SHAPE.format(
                            field=f'{label}.{field}',
                            received=received,
                        )
                    )
                    continue
                try:
                    if tuple(ref) in naturals.get(target_label, set()):
                        continue
                except TypeError:
                    # Unhashable components can never match a natural key.
                    pass
                if not self._resolves_in_db(target_label, ref):
                    errors.append(
                        f'object[{i}]: {label}.{field}={ref} does not '
                        f'resolve to a {target_label} row in the fixture '
                        'or the database',
                    )
        return errors

    def _missing_natural_key_fields(
        self, label: str, fields: dict, natural_key: tuple,
    ) -> list[str]:
        """Names of the natural-key positions absent from ``fields``.

        A position is absent when its key is missing from the payload
        (an absent key is always an error, even on nullable fields),
        when it resolves to ``None`` on a non-nullable field, or when it
        serialized as an empty string / empty list (the ``[]`` shape an
        unresolvable FK reference takes). An explicit ``None`` on a
        nullable model field is a legitimate NK NULL — base
        ``attributedef`` rows carry ``variant_key=None``, global defs
        carry ``node_type=None`` — and stays valid.

        ``natural_key`` is the tuple returned by ``natural_key_for``;
        its positions align with ``NATURAL_KEY_FIELDS[label]`` by
        construction. When the model cannot be resolved for the
        nullability map, every position is treated as required.
        """
        nullable = (
            self.fixture_repository.get_nullable_field_names(label)
            or frozenset()
        )
        missing = []
        for field_name, component in zip(
            NATURAL_KEY_FIELDS[label], natural_key,
        ):
            if (
                field_name not in fields
                or (component is None and field_name not in nullable)
                or component == ''
                or component == []
            ):
                missing.append(field_name)
        return missing

    def _missing_required_fields(
        self, label: str, fields: dict,
    ) -> Optional[list[str]]:
        """Names of required non-NK fields absent from ``fields``.

        Required means a concrete model field that is not the primary
        key, not ``null=True`` and has no default — the model is the
        source of truth via
        ``FixtureRepository.get_required_field_names``. Natural-key
        positions are excluded: ``_missing_natural_key_fields`` already
        reports them, and a missing field must not be flagged twice.

        A field is absent when its key is missing from ``fields`` or
        carries an explicit ``null`` — a non-nullable field cannot hold
        one. Any present value, even ``''`` or ``[]``, counts as
        present; degenerate values are caught by the shape and
        FK-resolution checks. Returns ``None`` when the model label
        does not resolve, so the caller skips the check entirely.
        """
        required = self.fixture_repository.get_required_field_names(label)
        if required is None:
            return None
        return [
            name
            for name in required - set(NATURAL_KEY_FIELDS.get(label, ()))
            if name not in fields or fields[name] is None
        ]

    def _resolves_in_db(self, target_label: str, ref: list) -> bool:
        """Natural-key reference resolves against the live database."""
        return self.fixture_repository.natural_key_exists(target_label, ref)

    @staticmethod
    def natural_key_for(label: str, fields: dict) -> Optional[tuple]:
        """Rebuild a row's natural key from its serialized fields.

        FK positions carry natural-key arrays in fixture format, so the
        first element is taken; plain scalars pass through unchanged.
        Returns ``None`` for models without a natural key (``node`` /
        ``nodeattribute`` are pk-referenced).
        """
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
