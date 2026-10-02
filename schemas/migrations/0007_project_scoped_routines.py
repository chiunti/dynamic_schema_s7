from importlib import import_module

from django.db import migrations


base = import_module('schemas.migrations.0002_s7_routines')


def _derive(sql: str, *pairs: tuple[str, str]) -> str:
    """Apply ``str.replace`` pairs that must all match.

    Scoped routines are derived from 0002's SQL text; if the base SQL drifts
    the derivation must fail loudly instead of shipping a silent no-op.
    """
    for old, new in pairs:
        if old not in sql:
            raise RuntimeError(
                "0007 scoped routine derivation failed: fragment not found "
                f"in base SQL: {old[:80]!r}..."
            )
        sql = sql.replace(old, new)
    return sql


SCOPED_IMPORT_SQL = _derive(
    base.FN_IMPORT_SCHEMA_JSON_SQL,
    (
        'CREATE OR REPLACE FUNCTION s7_import_schema(',
        'CREATE OR REPLACE FUNCTION s7_import_schema_scoped(',
    ),
    (
        "  RAISE NOTICE 's7_import_schema: key=%, version=%, status=%, project=%, org=%',\n"
        "    p_key, p_version, p_status, p_project_id, p_organization_id;",
        '',
    ),
    (
        "  IF p_schema IS NULL OR p_schema = 'null'::jsonb THEN",
        """  IF p_project_id IS NULL OR p_organization_id IS NULL OR NOT EXISTS (
    SELECT 1 FROM schema_projects
    WHERE id = p_project_id AND organization_id = p_organization_id
  ) THEN
    RAISE EXCEPTION 'Project and organization do not match';
  END IF;

  IF p_schema IS NULL OR p_schema = 'null'::jsonb THEN""",
    ),
    (
        '  v_existing_root_id := s7_find_schema_node_id(v_key_eff, v_version_eff);',
        '''  SELECT id INTO v_existing_root_id FROM schema_nodes
  WHERE key = v_key_eff AND version = v_version_eff
    AND project_id = p_project_id AND parent_id IS NULL
  LIMIT 1;''',
    ),
    (
        '        AND version = v_version_eff \n        AND parent_id IS NULL',
        '        AND version = v_version_eff \n        AND project_id = p_project_id\n        AND parent_id IS NULL',
    ),
    (
        '  -- Create root node with project_id and organization_id',
        '''  DELETE FROM schema_cache
  WHERE key = v_key_eff AND version = v_version_eff AND project_id = p_project_id;
  UPDATE schema_build_state SET dirty = TRUE
  WHERE key = v_key_eff AND version = v_version_eff AND project_id = p_project_id;

  -- Create root node with project_id and organization_id''',
    ),
)

SCOPED_CACHE_SQL = _derive(
    base.FN_BUILD_SCHEMA_CACHED_SQL,
    (
        'CREATE OR REPLACE FUNCTION s7_build_schema_cached(',
        'CREATE OR REPLACE FUNCTION s7_build_schema_cached_scoped(',
    ),
    (
        '  p_schema_type TEXT DEFAULT NULL\n)',
        '  p_schema_type TEXT,\n  p_project_id UUID\n)',
    ),
    (
        '    AND f.parent_id IS NULL\n  LIMIT 1;',
        '    AND f.parent_id IS NULL\n    AND f.project_id = p_project_id\n  LIMIT 1;',
    ),
)

SCOPED_PUBLISH_SQL = _derive(
    base.FN_PUBLISH_SCHEMA_SQL,
    (
        'CREATE OR REPLACE FUNCTION s7_publish_schema(p_key TEXT, p_version TEXT)',
        'CREATE OR REPLACE FUNCTION s7_publish_schema_scoped(p_key TEXT, p_version TEXT, p_project_id UUID)',
    ),
    (
        '    AND f.parent_id IS NULL\n  LIMIT 1;',
        '    AND f.parent_id IS NULL\n    AND f.project_id = p_project_id\n  LIMIT 1;',
    ),
    (
        "    AND f.parent_id IS NULL\n    AND na.value_string = 'published';",
        "    AND f.parent_id IS NULL\n    AND f.project_id = p_project_id\n    AND na.value_string = 'published';",
    ),
    (
        '  PERFORM s7_build_schema_cached(p_key, p_version);',
        '  PERFORM s7_build_schema_cached_scoped(p_key, p_version, NULL, p_project_id);',
    ),
)


class Migration(migrations.Migration):
    dependencies = [('schemas', '0006_project_credentials')]

    operations = [
        migrations.RunSQL(SCOPED_IMPORT_SQL,
                          reverse_sql='DROP FUNCTION IF EXISTS s7.s7_import_schema_scoped(JSONB, TEXT, TEXT, TEXT, BOOLEAN, UUID, UUID);'),
        migrations.RunSQL(SCOPED_CACHE_SQL,
                          reverse_sql='DROP FUNCTION IF EXISTS s7.s7_build_schema_cached_scoped(TEXT, TEXT, TEXT, UUID);'),
        migrations.RunSQL(SCOPED_PUBLISH_SQL,
                          reverse_sql='DROP FUNCTION IF EXISTS s7.s7_publish_schema_scoped(TEXT, TEXT, UUID);'),
    ]
