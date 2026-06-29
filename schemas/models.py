from django.db import models
from django.db.models import Q
from django.utils import timezone

import uuid


class Organization(models.Model):
    id = models.UUIDField(
        primary_key=True,
        editable=False,
        default=uuid.uuid4,
        db_default=models.Func(function="gen_random_uuid"),
    )
    name = models.CharField(max_length=255, unique=True)
    slug = models.SlugField(max_length=255, unique=True)
    description = models.TextField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "schema_organizations"
        indexes = [
            models.Index(fields=["slug"], name="idx_orgs_slug"),
            models.Index(fields=["is_active"], name="idx_orgs_active"),
        ]

    def __str__(self):
        return self.name


class OrganizationMember(models.Model):
    ROLE_CHOICES = [
        ('admin', 'Admin'),
        ('editor', 'Editor'),
        ('viewer', 'Viewer'),
    ]

    id = models.UUIDField(
        primary_key=True,
        editable=False,
        default=uuid.uuid4,
        db_default=models.Func(function="gen_random_uuid"),
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="members",
    )
    user = models.ForeignKey(
        'accounts.CustomUser',
        on_delete=models.CASCADE,
        related_name="organization_memberships",
    )
    role = models.CharField(max_length=50, choices=ROLE_CHOICES)
    joined_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "schema_organization_members"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "user"],
                name="uq_org_members_org_user",
            ),
        ]
        indexes = [
            models.Index(fields=["organization"], name="idx_org_members_org"),
            models.Index(fields=["user"], name="idx_org_members_user"),
        ]

    def __str__(self):
        return f"{self.user} — {self.organization} ({self.role})"


class Project(models.Model):
    id = models.UUIDField(
        primary_key=True,
        editable=False,
        default=uuid.uuid4,
        db_default=models.Func(function="gen_random_uuid"),
    )
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255)
    description = models.TextField(null=True, blank=True)
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        related_name="projects",
    )
    created_by = models.ForeignKey(
        'accounts.CustomUser',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_projects",
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "schema_projects"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "slug"],
                name="uq_projects_org_slug",
            ),
        ]
        indexes = [
            models.Index(fields=["slug"], name="idx_projects_slug"),
            models.Index(fields=["organization"], name="idx_projects_org"),
            models.Index(fields=["created_by"], name="idx_projects_creator"),
        ]

    def __str__(self):
        return self.name


class NodeType(models.Model):
    id = models.UUIDField(
        primary_key=True,
        editable=False,
        default=uuid.uuid4,
        db_default=models.Func(function="gen_random_uuid"),
    )
    name = models.CharField(max_length=255, unique=True)
    label = models.CharField(max_length=255)
    is_container = models.BooleanField(default=False)
    is_root = models.BooleanField(default=False)
    json_scope = models.TextField(null=True, blank=True)
    default_json_key = models.CharField(max_length=255, null=True, blank=True, help_text='Default JSON key for nodes of this type when no explicit key is provided')

    class Meta:
        db_table = "schema_node_types"

    def __str__(self):
        return self.name


class Node(models.Model):
    id = models.UUIDField(
        primary_key=True,
        editable=False,
        default=uuid.uuid4,
        db_default=models.Func(function="gen_random_uuid"),
    )
    sort_order = models.IntegerField(default=0)
    name = models.CharField(max_length=255, null=True, blank=True)
    key = models.CharField(max_length=255, null=True, blank=True)
    version = models.CharField(max_length=20, null=True, blank=True)
    node_type = models.ForeignKey(NodeType, on_delete=models.CASCADE)
    parent = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.DO_NOTHING,
    )
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="nodes",
    )
    organization = models.ForeignKey(
        Organization,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="nodes",
    )

    class Meta:
        db_table = "schema_nodes"
        indexes = [
            models.Index(fields=["parent", "sort_order"], name="idx_snodes_par_sort"),
            models.Index(fields=["name"], name="idx_snodes_name"),
            models.Index(fields=["parent"], name="idx_snodes_parent", condition=Q(parent__isnull=False)),
            models.Index(fields=["node_type", "parent"], name="idx_snodes_type_parent"),
            models.Index(fields=["organization"], name="idx_snodes_org"),
            models.Index(fields=["project"], name="idx_snodes_project"),
            models.Index(fields=["version"], name="idx_snodes_version"),
        ]

    def __str__(self):
        return self.name or str(self.id)


class Schema(Node):
    """Proxy model representing any root-level schema node.
    New schema types only require a seed migration — no code changes needed.
    """
    class Meta:
        proxy = True
        app_label = 'schemas'



class DataType(models.Model):
    id = models.UUIDField(
        primary_key=True,
        editable=False,
        default=uuid.uuid4,
        db_default=models.Func(function="gen_random_uuid"),
    )
    name = models.CharField(max_length=255, unique=True)
    description = models.CharField(max_length=255, null=True, blank=True)

    class Meta:
        db_table = "schema_data_types"

    def __str__(self):
        return self.name


class Domain(models.Model):
    id = models.UUIDField(
        primary_key=True,
        editable=False,
        default=uuid.uuid4,
        db_default=models.Func(function="gen_random_uuid"),
    )
    domain_name = models.CharField(max_length=255, unique=True)
    description = models.CharField(max_length=255, null=True, blank=True)

    class Meta:
        db_table = "schema_domains"

    def __str__(self):
        return self.domain_name


class DomainItem(models.Model):
    id = models.UUIDField(
        primary_key=True,
        editable=False,
        default=uuid.uuid4,
        db_default=models.Func(function="gen_random_uuid"),
    )
    value = models.CharField(max_length=255)
    label = models.CharField(max_length=255)
    domain = models.ForeignKey(Domain, on_delete=models.CASCADE)

    class Meta:
        db_table = "schema_domain_items"
        constraints = [
            models.UniqueConstraint(fields=["domain", "value"], name="uq_sdomain_items_value"),
        ]

    def __str__(self):
        return f"{self.domain.domain_name}:{self.value}"


class NodeTypeVariant(models.Model):
    id = models.UUIDField(
        primary_key=True,
        editable=False,
        default=uuid.uuid4,
        db_default=models.Func(function="gen_random_uuid"),
    )
    node_type = models.ForeignKey(NodeType, on_delete=models.CASCADE, related_name="variants")
    variant_key = models.CharField(max_length=255)
    discriminator_attr = models.CharField(
        max_length=255,
        default='type',
        null=True,
        blank=True,
        help_text='Attribute name that discriminates this variant (e.g., type, input_mode, widget_class)'
    )
    props_node_type = models.ForeignKey(
        NodeType,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='+',
        help_text='Child node type that holds variant-specific props (null = parent node itself)'
    )

    class Meta:
        db_table = "schema_node_type_variants"
        constraints = [
            models.UniqueConstraint(fields=["node_type", "variant_key", "discriminator_attr"], name="uq_snode_type_variants_nt_key_disc"),
        ]

    def __str__(self):
        return f"{self.node_type.name}:{self.variant_key}"


class AttributeDef(models.Model):
    id = models.UUIDField(
        primary_key=True,
        editable=False,
        default=uuid.uuid4,
        db_default=models.Func(function="gen_random_uuid"),
    )
    name = models.CharField(max_length=255)
    json_key = models.TextField()
    is_required = models.BooleanField(default=False)
    is_common = models.BooleanField(default=False)
    variant_key = models.CharField(max_length=255, null=True, blank=True)
    data_type = models.ForeignKey(DataType, on_delete=models.CASCADE)
    domain = models.ForeignKey(Domain, null=True, blank=True, on_delete=models.SET_NULL)
    node_type = models.ForeignKey(NodeType, null=True, blank=True, on_delete=models.SET_NULL)
    group = models.CharField(max_length=64, null=True, blank=True)

    class Meta:
        db_table = "schema_attribute_defs"
        indexes = [
            models.Index(fields=["node_type", "json_key"], name="idx_attr_defs_type_json"),
            models.Index(fields=["node_type", "variant_key"], name="idx_attr_defs_type_variant"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["node_type", "json_key", "variant_key"],
                name="uq_sattr_defs_key",
            ),
            models.UniqueConstraint(
                fields=["node_type", "name", "variant_key"],
                name="uq_attr_defs_nt_name",
            ),
        ]

    def __str__(self):
        if self.node_type_id:
            if self.variant_key:
                return f"{self.node_type.name}[{self.variant_key}]:{self.name}"
            return f"{self.node_type.name}:{self.name}"
        return self.name


class NodeAttribute(models.Model):
    id = models.UUIDField(
        primary_key=True,
        editable=False,
        default=uuid.uuid4,
        db_default=models.Func(function="gen_random_uuid"),
    )
    value_string = models.CharField(max_length=255, null=True, blank=True)
    value_number = models.DecimalField(null=True, blank=True, max_digits=30, decimal_places=10)
    value_bool = models.BooleanField(null=True, blank=True)
    value_json = models.JSONField(null=True, blank=True)
    attribute_def = models.ForeignKey(AttributeDef, on_delete=models.CASCADE)
    node = models.ForeignKey(Node, on_delete=models.CASCADE)

    class Meta:
        db_table = "schema_node_attributes"
        indexes = [
            models.Index(fields=["node"], name="idx_snode_attrs_node"),
            models.Index(fields=["attribute_def"], name="idx_snode_attrs_attr"),
        ]
        constraints = [
            models.UniqueConstraint(fields=["node", "attribute_def"], name="uq_snode_attrs_node_attr"),
            models.CheckConstraint(
                name="chk_single_value",
                check=(
                    # Exactly one value field is non-null
                    (Q(value_string__isnull=False) & Q(value_number__isnull=True) & Q(value_bool__isnull=True) & Q(value_json__isnull=True))
                    | (Q(value_string__isnull=True) & Q(value_number__isnull=False) & Q(value_bool__isnull=True) & Q(value_json__isnull=True))
                    | (Q(value_string__isnull=True) & Q(value_number__isnull=True) & Q(value_bool__isnull=False) & Q(value_json__isnull=True))
                    | (Q(value_string__isnull=True) & Q(value_number__isnull=True) & Q(value_bool__isnull=True) & Q(value_json__isnull=False))
                    # OR all values are null (for attributes that exist but have no value set)
                    | (Q(value_string__isnull=True) & Q(value_number__isnull=True) & Q(value_bool__isnull=True) & Q(value_json__isnull=True))
                ),
            ),
        ]

    def __str__(self):
        return f"{self.node.name or str(self.node.id)}:{self.attribute_def.name}"


class NodeTypeComposition(models.Model):
    id = models.UUIDField(
        primary_key=True,
        editable=False,
        default=uuid.uuid4,
        db_default=models.Func(function="gen_random_uuid"),
    )
    collection_key = models.CharField(max_length=255, null=True, blank=True)
    min_children = models.IntegerField(null=True, blank=True)
    max_children = models.IntegerField(null=True, blank=True)
    child_type = models.ForeignKey(NodeType, on_delete=models.CASCADE, related_name="child_compositions")
    parent_type = models.ForeignKey(NodeType, on_delete=models.CASCADE, related_name="parent_compositions")

    class Meta:
        db_table = "schema_node_type_compositions"
        constraints = [
            models.UniqueConstraint(fields=["parent_type", "child_type", "collection_key"], name="uq_snode_type_comps"),
        ]

    def __str__(self):
        return f"{self.parent_type.name} -> {self.child_type.name}"


class BuildState(models.Model):
    id = models.UUIDField(
        primary_key=True,
        editable=False,
        default=uuid.uuid4,
        db_default=models.Func(function="gen_random_uuid"),
    )
    key = models.CharField(max_length=30)
    version = models.CharField(max_length=20)
    current_build = models.BigIntegerField(default=1)
    last_cached_build = models.BigIntegerField(null=True, blank=True)
    dirty = models.BooleanField(default=False)
    updated_at = models.DateTimeField(default=timezone.now, editable=False)
    cached_at = models.DateTimeField(null=True, blank=True, editable=False)
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="build_states",
    )

    class Meta:
        db_table = "schema_build_state"
        verbose_name = "Schema Build State"
        verbose_name_plural = "Schema Build States"
        constraints = [
            models.UniqueConstraint(
                fields=["key", "version", "project"],
                name="uq_schema_build_state_key_version_project",
            ),
        ]
        indexes = [
            models.Index(fields=["updated_at"], name="idx_schema_build_state_updated"),
            models.Index(fields=["project"], name="idx_build_state_project"),
        ]

    def __str__(self):
        return f"{self.key}:{self.version}"


class SchemaCache(models.Model):

    id = models.UUIDField(
        primary_key=True,
        editable=False,
        default=uuid.uuid4,
        db_default=models.Func(function="gen_random_uuid"),
    )
    key = models.CharField(max_length=30)
    version = models.CharField(max_length=20)
    schema_json = models.JSONField()
    generated_at = models.DateTimeField(default=timezone.now, editable=False)
    schema_type = models.CharField(max_length=20, null=True, blank=True)
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="schema_caches",
    )

    class Meta:
        db_table = "schema_cache"
        verbose_name = "Schema Cache"
        verbose_name_plural = "Schema Caches"
        indexes = [
            models.Index(fields=["generated_at"], name="idx_schema_cache_generated_at"),
            models.Index(fields=["schema_type", "key", "version"], name="idx_schema_cache_type_key_ver"),
            models.Index(fields=["key", "version"], name="idx_schema_cache_key_ver"),
            models.Index(fields=["project"], name="idx_schema_cache_project"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["key", "version", "project"],
                name="uq_schema_cache_key_version_project",
            ),
        ]

    def __str__(self):
        return f"{self.key}:{self.version}"


class ComponentPropertiesProxy(Domain):
    class Meta:
        proxy = True
        verbose_name = "Component Properties"
        verbose_name_plural = "Component Properties"
