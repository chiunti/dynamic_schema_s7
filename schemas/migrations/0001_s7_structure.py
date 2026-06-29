import uuid

from django.conf import settings
from django.utils import timezone
from django.contrib.postgres.operations import CreateExtension
from django.db import migrations, models
from django.db.models import Q, Func
import django.db.models.deletion

class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        CreateExtension("pgcrypto"),
        migrations.RunSQL("CREATE SCHEMA IF NOT EXISTS s7;"),
        migrations.RunSQL("SET search_path TO s7, public;"),
        # ------------------------------
        # Organization
        # ------------------------------
        migrations.CreateModel(
            name="Organization",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        primary_key=True,
                        serialize=False,
                        editable=False,
                        default=uuid.uuid4,
                        db_default=Func(function='gen_random_uuid'),
                    ),
                ),
                ("name", models.CharField(max_length=255, unique=True)),
                ("slug", models.SlugField(max_length=255, unique=True)),
                ("description", models.TextField(null=True, blank=True)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(default=timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "db_table": "schema_organizations",
            },
        ),
        migrations.AddIndex(
            model_name="organization",
            index=models.Index(fields=["slug"], name="idx_orgs_slug"),
        ),
        migrations.AddIndex(
            model_name="organization",
            index=models.Index(fields=["is_active"], name="idx_orgs_active"),
        ),
        # ------------------------------
        # OrganizationMember
        # ------------------------------
        migrations.CreateModel(
            name="OrganizationMember",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        primary_key=True,
                        serialize=False,
                        editable=False,
                        default=uuid.uuid4,
                        db_default=Func(function='gen_random_uuid'),
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="schemas.organization",
                        related_name="members",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to=settings.AUTH_USER_MODEL,
                        related_name="organization_memberships",
                    ),
                ),
                ("role", models.CharField(max_length=50, choices=[("admin", "Admin"), ("editor", "Editor"), ("viewer", "Viewer")])),
                ("joined_at", models.DateTimeField(default=timezone.now)),
            ],
            options={
                "db_table": "schema_organization_members",
            },
        ),
        migrations.AddConstraint(
            model_name="organizationmember",
            constraint=models.UniqueConstraint(
                fields=["organization", "user"],
                name="uq_org_members_org_user",
            ),
        ),
        migrations.AddIndex(
            model_name="organizationmember",
            index=models.Index(fields=["organization"], name="idx_org_members_org"),
        ),
        migrations.AddIndex(
            model_name="organizationmember",
            index=models.Index(fields=["user"], name="idx_org_members_user"),
        ),
        # ------------------------------
        # Project
        # ------------------------------
        migrations.CreateModel(
            name="Project",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        primary_key=True,
                        serialize=False,
                        editable=False,
                        default=uuid.uuid4,
                        db_default=Func(function='gen_random_uuid'),
                    ),
                ),
                ("name", models.CharField(max_length=255)),
                ("slug", models.SlugField(max_length=255)),
                ("description", models.TextField(null=True, blank=True)),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="schemas.organization",
                        related_name="projects",
                    ),
                ),
                (
                    "created_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.SET_NULL,
                        to=settings.AUTH_USER_MODEL,
                        null=True,
                        blank=True,
                        related_name="created_projects",
                    ),
                ),
                ("created_at", models.DateTimeField(default=timezone.now)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "db_table": "schema_projects",
            },
        ),
        migrations.AddConstraint(
            model_name="project",
            constraint=models.UniqueConstraint(
                fields=["organization", "slug"],
                name="uq_projects_org_slug",
            ),
        ),
        migrations.AddIndex(
            model_name="project",
            index=models.Index(fields=["slug"], name="idx_projects_slug"),
        ),
        migrations.AddIndex(
            model_name="project",
            index=models.Index(fields=["organization"], name="idx_projects_org"),
        ),
        migrations.AddIndex(
            model_name="project",
            index=models.Index(fields=["created_by"], name="idx_projects_creator"),
        ),
        # ------------------------------
        # NodeType
        # ------------------------------
        migrations.CreateModel(
            name="NodeType",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        primary_key=True,
                        serialize=False,
                        editable=False,
                        default=uuid.uuid4,
                        db_default=Func(function='gen_random_uuid'),
                    ),
                ),
                ("name", models.CharField(max_length=255, unique=True)),
                ("label", models.CharField(max_length=255)),
                ("is_container", models.BooleanField(default=False)),
                ("is_root", models.BooleanField(default=False)),
                ("json_scope", models.TextField(null=True, blank=True)),
                ("default_json_key", models.CharField(max_length=255, null=True, blank=True, help_text='Default JSON key for nodes of this type when no explicit key is provided')),
            ],
            options={
                "db_table": "schema_node_types",
            },
        ),
        # ------------------------------
        # Node
        # ------------------------------
        migrations.CreateModel(
            name="Node",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        primary_key=True,
                        serialize=False,
                        editable=False,
                        default=uuid.uuid4,
                        db_default=Func(function='gen_random_uuid'),
                    ),
                ),
                ("sort_order", models.IntegerField(default=0)),
                ("name", models.CharField(max_length=255, null=True, blank=True)),
                ("key", models.CharField(max_length=255, null=True, blank=True)),
                ("version", models.CharField(max_length=20, null=True, blank=True)),
                (
                    "node_type",
                    models.ForeignKey(
                        on_delete=models.CASCADE,
                        to="schemas.nodetype",
                    ),
                ),
                (
                    "parent",
                    models.ForeignKey(
                        null=True,
                        blank=True,
                        on_delete=models.DO_NOTHING,
                        to="schemas.node",
                    ),
                ),
                (
                    "project",
                    models.ForeignKey(
                        null=True,
                        blank=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        to="schemas.project",
                        related_name="nodes",
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        null=True,
                        blank=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        to="schemas.organization",
                        related_name="nodes",
                    ),
                ),
            ],
            options={
                "db_table": "schema_nodes",
            },
        ),
        migrations.AddIndex(
            model_name="node",
            index=models.Index(fields=["parent", "sort_order"], name="idx_snodes_par_sort"),
        ),
        migrations.AddIndex(
            model_name="node",
            index=models.Index(fields=["name"], name="idx_snodes_name"),
        ),
        migrations.AddIndex(
            model_name="node",
            index=models.Index(fields=["parent"], name="idx_snodes_parent", condition=Q(parent__isnull=False)),
        ),
        migrations.AddIndex(
            model_name="node",
            index=models.Index(fields=["node_type", "parent"], name="idx_snodes_type_parent"),
        ),
        migrations.AddIndex(
            model_name="node",
            index=models.Index(fields=["organization"], name="idx_snodes_org"),
        ),
        migrations.AddIndex(
            model_name="node",
            index=models.Index(fields=["project"], name="idx_snodes_project"),
        ),
        migrations.AddIndex(
            model_name="node",
            index=models.Index(fields=["version"], name="idx_snodes_version"),
        ),
        migrations.RunSQL(
            sql="""
            CREATE UNIQUE INDEX uq_snodes_key_version_root
              ON s7.schema_nodes (key, version)
              WHERE parent_id IS NULL AND version IS NOT NULL;
            """,
            reverse_sql="DROP INDEX IF EXISTS s7.uq_snodes_key_version_root;",
        ),
        # ------------------------------
        # DataType
        # ------------------------------
        migrations.CreateModel(
            name="DataType",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        primary_key=True,
                        serialize=False,
                        editable=False,
                        default=uuid.uuid4,
                        db_default=Func(function='gen_random_uuid'),
                    ),
                ),
                ("name", models.CharField(max_length=255, unique=True)),
                ("description", models.CharField(max_length=255, null=True, blank=True)),
            ],
            options={
                "db_table": "schema_data_types",
            },
        ),
        # ------------------------------
        # Domain
        # ------------------------------
        migrations.CreateModel(
            name="Domain",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        primary_key=True,
                        serialize=False,
                        editable=False,
                        default=uuid.uuid4,
                        db_default=Func(function='gen_random_uuid'),
                    ),
                ),
                ("domain_name", models.CharField(max_length=255, unique=True)),
                ("description", models.CharField(max_length=255, null=True, blank=True)),
            ],
            options={
                "db_table": "schema_domains",
            },
        ),
        # ------------------------------
        # DomainItem
        # ------------------------------
        migrations.CreateModel(
            name="DomainItem",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        primary_key=True,
                        serialize=False,
                        editable=False,
                        default=uuid.uuid4,
                        db_default=Func(function='gen_random_uuid'),
                    ),
                ),
                ("value", models.CharField(max_length=255)),
                ("label", models.CharField(max_length=255)),
                (
                    "domain",
                    models.ForeignKey(
                        on_delete=models.CASCADE,
                        to="schemas.domain",
                    ),
                ),
            ],
            options={
                "db_table": "schema_domain_items",
            },
        ),
        migrations.AddConstraint(
            model_name="domainitem",
            constraint=models.UniqueConstraint(fields=["domain", "value"], name="uq_sdomain_items_value"),
        ),
        # ------------------------------
        # AttributeDef
        # ------------------------------
        migrations.CreateModel(
            name="AttributeDef",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        primary_key=True,
                        serialize=False,
                        editable=False,
                        default=uuid.uuid4,
                        db_default=Func(function='gen_random_uuid'),
                    ),
                ),
                ("name", models.CharField(max_length=255)),
                ("json_key", models.TextField()),
                ("is_required", models.BooleanField(default=False)),
                ("is_common", models.BooleanField(default=False)),
                ("variant_key", models.CharField(max_length=255, null=True, blank=True)),
                ("group", models.CharField(max_length=64, null=True, blank=True)),
                (
                    "data_type",
                    models.ForeignKey(
                        on_delete=models.CASCADE,
                        to="schemas.datatype",
                    ),
                ),
                (
                    "domain",
                    models.ForeignKey(
                        null=True,
                        blank=True,
                        on_delete=models.SET_NULL,
                        to="schemas.domain",
                    ),
                ),
                (
                    "node_type",
                    models.ForeignKey(
                        null=True,
                        blank=True,
                        on_delete=models.SET_NULL,
                        to="schemas.nodetype",
                    ),
                ),
            ],
            options={
                "db_table": "schema_attribute_defs",
            },
        ),
        migrations.AddIndex(
            model_name="attributedef",
            index=models.Index(fields=["node_type", "json_key"], name="idx_attr_defs_type_json"),
        ),
        migrations.AddIndex(
            model_name="attributedef",
            index=models.Index(fields=["node_type", "variant_key"], name="idx_attr_defs_type_variant"),
        ),
        migrations.AddConstraint(
            model_name="attributedef",
            constraint=models.UniqueConstraint(
                fields=["node_type", "name", "variant_key"],
                name="uq_attr_defs_nt_name",
            ),
        ),
        migrations.AddConstraint(
            model_name="attributedef",
            constraint=models.UniqueConstraint(
                fields=["node_type", "json_key", "variant_key"],
                name="uq_sattr_defs_key",
            ),
        ),
        # ------------------------------
        # NodeAttribute
        # ------------------------------
        migrations.CreateModel(
            name="NodeAttribute",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        primary_key=True,
                        serialize=False,
                        editable=False,
                        default=uuid.uuid4,
                        db_default=Func(function='gen_random_uuid'),
                    ),
                ),
                ("value_string", models.CharField(max_length=255, null=True, blank=True)),
                ("value_number", models.DecimalField(null=True, blank=True, max_digits=30, decimal_places=10)),
                ("value_bool", models.BooleanField(null=True, blank=True)),
                ("value_json", models.JSONField(null=True, blank=True)),
                (
                    "attribute_def",
                    models.ForeignKey(
                        on_delete=models.CASCADE,
                        to="schemas.attributedef",
                    ),
                ),
                (
                    "node",
                    models.ForeignKey(
                        on_delete=models.CASCADE,
                        to="schemas.node",
                    ),
                ),
            ],
            options={
                "db_table": "schema_node_attributes",
            },
        ),
        migrations.AddIndex(
            model_name="nodeattribute",
            index=models.Index(fields=["node"], name="idx_snode_attrs_node"),
        ),
        migrations.AddIndex(
            model_name="nodeattribute",
            index=models.Index(fields=["attribute_def"], name="idx_snode_attrs_attr"),
        ),
        migrations.AddConstraint(
            model_name="nodeattribute",
            constraint=models.UniqueConstraint(fields=["node", "attribute_def"], name="uq_snode_attrs_node_attr"),
        ),
        migrations.AddConstraint(
            model_name="nodeattribute",
            constraint=models.CheckConstraint(
                name="chk_single_value",
                check=(
                    (Q(value_string__isnull=False) & Q(value_number__isnull=True) & Q(value_bool__isnull=True) & Q(value_json__isnull=True))
                    | (Q(value_string__isnull=True) & Q(value_number__isnull=False) & Q(value_bool__isnull=True) & Q(value_json__isnull=True))
                    | (Q(value_string__isnull=True) & Q(value_number__isnull=True) & Q(value_bool__isnull=False) & Q(value_json__isnull=True))
                    | (Q(value_string__isnull=True) & Q(value_number__isnull=True) & Q(value_bool__isnull=True) & Q(value_json__isnull=False))
                    | (Q(value_string__isnull=True) & Q(value_number__isnull=True) & Q(value_bool__isnull=True) & Q(value_json__isnull=True))
                ),
            ),
        ),
        # ------------------------------
        # NodeTypeComposition
        # ------------------------------
        migrations.CreateModel(
            name="NodeTypeComposition",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        primary_key=True,
                        serialize=False,
                        editable=False,
                        default=uuid.uuid4,
                        db_default=Func(function='gen_random_uuid'),
                    ),
                ),
                ("collection_key", models.CharField(max_length=255, null=True, blank=True)),
                ("min_children", models.IntegerField(null=True, blank=True)),
                ("max_children", models.IntegerField(null=True, blank=True)),
                (
                    "child_type",
                    models.ForeignKey(
                        on_delete=models.CASCADE,
                        to="schemas.nodetype",
                        related_name="child_compositions",
                    ),
                ),
                (
                    "parent_type",
                    models.ForeignKey(
                        on_delete=models.CASCADE,
                        to="schemas.nodetype",
                        related_name="parent_compositions",
                    ),
                ),
            ],
            options={
                "db_table": "schema_node_type_compositions",
            },
        ),
        migrations.AddConstraint(
            model_name="nodetypecomposition",
            constraint=models.UniqueConstraint(fields=["parent_type", "child_type", "collection_key"], name="uq_snode_type_comps"),
        ),
        # ------------------------------
        # NodeTypeVariant
        # ------------------------------
        migrations.CreateModel(
            name="NodeTypeVariant",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        primary_key=True,
                        serialize=False,
                        editable=False,
                        default=uuid.uuid4,
                        db_default=Func(function='gen_random_uuid'),
                    ),
                ),
                (
                    "node_type",
                    models.ForeignKey(
                        on_delete=models.CASCADE,
                        to="schemas.nodetype",
                        related_name="variants",
                    ),
                ),
                ("variant_key", models.CharField(max_length=255)),
                (
                    "discriminator_attr",
                    models.CharField(
                        max_length=255,
                        default='type',
                        null=True,
                        blank=True,
                        help_text='Attribute name that discriminates this variant (e.g., type, input_mode, widget_class)'
                    ),
                ),
                (
                    "props_node_type",
                    models.ForeignKey(
                        on_delete=models.SET_NULL,
                        null=True,
                        blank=True,
                        to="schemas.nodetype",
                        related_name='+',
                        help_text='Child node type that holds variant-specific props (null = parent node itself)'
                    ),
                ),
            ],
            options={
                "db_table": "schema_node_type_variants",
            },
        ),
        migrations.AddConstraint(
            model_name="nodetypevariant",
            constraint=models.UniqueConstraint(
                fields=["node_type", "variant_key", "discriminator_attr"],
                name="uq_snode_type_variants_nt_key_disc",
            ),
        ),
        # ------------------------------
        # BuildState
        # ------------------------------
        migrations.CreateModel(
            name="BuildState",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        primary_key=True,
                        serialize=False,
                        editable=False,
                        default=uuid.uuid4,
                        db_default=Func(function='gen_random_uuid'),
                    ),
                ),
                ("key", models.CharField(max_length=30)),
                ("version", models.CharField(max_length=20)),
                ("current_build", models.BigIntegerField(default=1)),
                ("last_cached_build", models.BigIntegerField(null=True, blank=True)),
                ("dirty", models.BooleanField(default=False)),
                ("updated_at", models.DateTimeField(default=timezone.now, editable=False)),
                ("cached_at", models.DateTimeField(null=True, blank=True, editable=False)),
                (
                    "project",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="schemas.project",
                        related_name="build_states",
                    ),
                ),
            ],
            options={
                "db_table": "schema_build_state",
                "verbose_name": "Schema Build State",
                "verbose_name_plural": "Schema Build States",
            },
        ),
        migrations.AddIndex(
            model_name="buildstate",
            index=models.Index(fields=["updated_at"], name="idx_schema_build_state_updated"),
        ),
        migrations.AddIndex(
            model_name="buildstate",
            index=models.Index(fields=["project"], name="idx_build_state_project"),
        ),
        migrations.AddConstraint(
            model_name="buildstate",
            constraint=models.UniqueConstraint(fields=["key", "version", "project"], name="uq_schema_build_state_key_version_project"),
        ),
        # ------------------------------
        # SchemaCache
        # ------------------------------
        migrations.CreateModel(
            name="SchemaCache",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        primary_key=True,
                        serialize=False,
                        editable=False,
                        default=uuid.uuid4,
                        db_default=Func(function='gen_random_uuid'),
                    ),
                ),
                ("key", models.CharField(max_length=30)),
                ("version", models.CharField(max_length=20)),
                ("schema_json", models.JSONField()),
                ("schema_type", models.CharField(max_length=20, null=True, blank=True)),
                ("generated_at", models.DateTimeField(default=timezone.now, editable=False)),
                (
                    "project",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="schemas.project",
                        related_name="schema_caches",
                    ),
                ),
            ],
            options={
                "db_table": "schema_cache",
                "verbose_name": "Schema Cache",
                "verbose_name_plural": "Schema Caches",
            },
        ),
        migrations.AddIndex(
            model_name="schemacache",
            index=models.Index(fields=["generated_at"], name="idx_schema_cache_generated_at"),
        ),
        migrations.AddIndex(
            model_name="schemacache",
            index=models.Index(fields=["schema_type", "key", "version"], name="idx_schema_cache_type_key_ver"),
        ),
        migrations.AddIndex(
            model_name="schemacache",
            index=models.Index(fields=["project"], name="idx_schema_cache_project"),
        ),
        migrations.AddConstraint(
            model_name="schemacache",
            constraint=models.UniqueConstraint(fields=["key", "version", "project"], name="uq_schema_cache_key_version_project"),
        ),
        # ------------------------------
        # Proxy models
        # ------------------------------
        migrations.CreateModel(
            name="Schema",
            fields=[],
            options={
                "proxy": True,
                "indexes": [],
                "constraints": [],
            },
            bases=("schemas.node",),
        ),
        migrations.CreateModel(
            name="ComponentPropertiesProxy",
            fields=[],
            options={
                "verbose_name": "Component Properties",
                "verbose_name_plural": "Component Properties",
                "proxy": True,
                "indexes": [],
                "constraints": [],
            },
            bases=("schemas.domain",),
        ),
    ]
