"""
Business logic for AttributeDef, component type, and variant property management.
"""

from django.db import transaction

from ..repositories.attribute_def_repository import AttributeDefRepository
from ..repositories.node_type_repository import NodeTypeRepository
from ..constants import (
    ERR_SCOPE_REQUIRED,
    ERR_SCOPE_NOT_FOUND,
    ERR_COMPONENT_TYPE_DOMAIN_NOT_FOUND,
    ERR_VARIANT_KEY_NOT_FOUND,
    ERR_DEFAULT_DATA_TYPE_NOT_FOUND,
    ERR_ATTRIBUTE_DEF_NOT_FOUND,
    ERR_ALREADY_SPECIFIC,
    ERR_JSON_KEY_DUPLICATE,
    ERR_JSON_KEY_AND_NAME_REQUIRED,
    ERR_PROPERTIES_MUST_BE_ARRAY,
    ERR_VARIANT_COLUMN_CARRIER_MSG,
    ERR_VARIANT_NODE_FIELD_CARRIER_MSG,
    ERR_VARIANT_VERSION_CARRIER_MSG,
)
from .datatype_plugins import (
    _AUTHOR_SETTABLE_NODE_FIELDS,
    is_column_mapped_datatype,
)


class AttributeDefService:
    """Service for component property and attribute definition business logic."""

    def __init__(self):
        self.repository = AttributeDefRepository()
        self.node_type_repository = NodeTypeRepository()

    def get_component_types_for_scope(self, scope: str) -> dict:
        """
        Return the list of component types (DomainItems) reachable from a root scope.

        The result is the union of all candidate discriminator domains,
        deduplicated by ``value``: sibling defs may declare the same
        discriminator json_key under different ``variant_key``s — each
        with its own domain — and several scope type_ids may carry a
        discriminator, so every domain-bound candidate may legitimately
        own component-type rows. Unlike the AI-113 existence gate, this
        endpoint materializes a list, so all candidates contribute their
        items: the first occurrence in candidate order wins on duplicate
        values, and the final list is sorted by ``value`` for
        determinism.

        Raises ValueError if scope is missing; LookupError if the scope
        is not found or no type domain exists.
        """
        if not scope:
            raise ValueError(ERR_SCOPE_REQUIRED)

        type_ids = self.node_type_repository.get_node_type_ids_for_scope(scope)
        if not type_ids:
            raise LookupError(ERR_SCOPE_NOT_FOUND)
        attr_defs = self.repository.get_type_attribute_defs_for_type_ids(type_ids)
        if not attr_defs:
            raise LookupError(ERR_COMPONENT_TYPE_DOMAIN_NOT_FOUND)

        items_by_value: dict[str, dict] = {}
        for attr_def in attr_defs:
            for item in self.repository.get_component_types_for_domain(attr_def.domain):
                items_by_value.setdefault(item["value"], item)
        component_types = [items_by_value[value] for value in sorted(items_by_value)]
        return {"component_types": component_types}

    def get_component_properties(self, component_type: str) -> dict:
        """Return the property DomainItems for a given component type."""
        domain_name = f"properties_{component_type}"
        domain = self.repository.get_domain_by_name(domain_name)
        if not domain:
            return {"domain_name": domain_name, "properties": []}
        properties = self.repository.get_domain_items(domain)
        return {"domain_name": domain_name, "properties": properties}

    @transaction.atomic
    def save_component_properties(self, component_type: str, properties: list) -> dict:
        """
        Replace the DomainItems for a component type domain and sync variant AttributeDefs.
        Raises TypeError if properties is not a list.
        Raises LookupError if the default DataType is missing.
        Raises ValueError when a property name matches a Node column
        (key/name/sort_order/version): the minted def would be a
        variant-scoped name-match — the declaration shape the other
        gates reject, inert under the field-map's IS NULL rule (AI-147).
        """
        if not isinstance(properties, list):
            raise TypeError(ERR_PROPERTIES_MUST_BE_ARRAY)

        domain_name = f"properties_{component_type}"
        domain, _ = self.repository.get_or_create_domain(
            domain_name, f"Properties for component type: {component_type}"
        )
        self.repository.delete_domain_items(domain)

        node_type_ids = self.repository.get_all_node_type_ids()
        catalog_attrs = self.repository.get_catalog_attribute_defs(node_type_ids)

        current_variant_json_keys = {
            a["json_key"] for a in self.repository.get_variant_attribute_defs(node_type_ids, component_type)
        }

        to_delete = current_variant_json_keys - set(properties)
        if to_delete:
            self.repository.delete_variant_attribute_defs(node_type_ids, component_type, to_delete)

        domain_items_to_create = []
        for prop_value in properties:
            attr_def = catalog_attrs.get(prop_value)
            if not attr_def:
                nt = self.repository.get_any_node_type()
                if nt:
                    # A prop named after a Node column would mint a
                    # variant-scoped def whose name collides with the
                    # column — the declaration shape the service-layer
                    # gates reject (AI-141): the field-map resolves
                    # column carriers at variant_key IS NULL scope only,
                    # so the def could never write it. The prop is
                    # invalid, not persisted inert — reject it where it
                    # is born. A prop resolving to a catalog def never
                    # reaches this branch and stays legitimate.
                    if prop_value in _AUTHOR_SETTABLE_NODE_FIELDS:
                        raise ValueError(
                            ERR_VARIANT_NODE_FIELD_CARRIER_MSG.format(
                                natural_key=[
                                    nt.name, prop_value, component_type,
                                ],
                                name=prop_value,
                                variant_key=component_type,
                            )
                        )
                    dt = self.repository.get_default_data_type()
                    if not dt:
                        raise LookupError(ERR_DEFAULT_DATA_TYPE_NOT_FOUND)
                    attr_def = self.repository.create_attribute_def(
                        node_type=nt,
                        name=prop_value,
                        json_key=prop_value,
                        data_type=dt,
                        is_required=False,
                        is_common=False,
                        variant_key=component_type,
                    )
            label = attr_def.name if attr_def else prop_value
            domain_items_to_create.append({"value": prop_value, "label": label})

        if domain_items_to_create:
            self.repository.bulk_create_domain_items(domain, domain_items_to_create)

        return {
            "ok": True,
            "domain_name": domain_name,
            "properties_count": len(domain_items_to_create),
        }

    def get_variants_for_scope(self, scope: str) -> dict:
        """Return all NodeTypeVariants reachable from a given root scope."""
        type_ids = self.node_type_repository.get_node_type_ids_for_scope(scope) if scope else None
        variants = self.repository.get_all_variants(type_ids)
        return {"variants": variants}

    def get_variants_for_node_type(self, node_type_id: str) -> list[str]:
        """Return variant keys for a specific node_type."""
        return self.repository.get_variants_for_node_type(node_type_id)

    @transaction.atomic
    def create_attribute_def(
        self,
        variant_key: str,
        json_key: str,
        name: str,
        is_required: bool = False,
        is_common: bool = False,
        add_to_catalog: bool = False,
    ) -> dict:
        """
        Create a new AttributeDef for a variant, common pool, or catalog.
        Raises LookupError if the variant_key has no associated NodeType.
        Raises LookupError if the default DataType is missing.
        Raises ValueError if json_key+node_type+variant_key combination already exists.
        Raises ValueError if name matches a Node column
        (key/name/sort_order/version) while the def stays
        variant-scoped: the field-map resolves Node-column carriers at
        variant_key IS NULL scope only, so the declaration would be
        inert. Common (is_common) and catalog (add_to_catalog) defs
        keep variant_key=None and remain legitimate carriers. Mirrors
        the catalog-import rejection of the same declaration shape
        (AI-141).
        """
        if not json_key or not name:
            raise ValueError(ERR_JSON_KEY_AND_NAME_REQUIRED)

        if not self.repository.variant_key_exists(variant_key):
            raise LookupError(ERR_VARIANT_KEY_NOT_FOUND)

        node_type = self.repository.get_node_type_by_variant(variant_key)

        effective_variant_key = variant_key
        if is_common or add_to_catalog:
            effective_variant_key = None

        # Reject a Node-column name under a variant: the field-map
        # resolves column carriers at variant_key IS NULL scope only,
        # so a variant-scoped def named after a column could never
        # write it — the declaration is invalid, not persisted inert.
        if (effective_variant_key is not None
                and name in _AUTHOR_SETTABLE_NODE_FIELDS):
            raise ValueError(
                ERR_VARIANT_NODE_FIELD_CARRIER_MSG.format(
                    natural_key=[
                        node_type.name, json_key, effective_variant_key,
                    ],
                    name=name,
                    variant_key=effective_variant_key,
                )
            )

        if self.repository.attribute_def_exists(node_type, json_key, effective_variant_key):
            raise ValueError(ERR_JSON_KEY_DUPLICATE)

        dt = self.repository.get_default_data_type()
        if not dt:
            raise LookupError(ERR_DEFAULT_DATA_TYPE_NOT_FOUND)

        attr_def = self.repository.create_attribute_def(
            node_type=node_type,
            name=name,
            json_key=json_key,
            data_type=dt,
            is_required=is_required,
            is_common=is_common,
            variant_key=effective_variant_key,
        )
        return {"ok": True, "id": str(attr_def.id)}

    @transaction.atomic
    def make_attribute_common(self, attr_id: str) -> dict:
        """
        Convert a variant-specific AttributeDef to common (is_common=True, variant_key=None).
        Raises LookupError if not found.
        """
        attr_def = self.repository.get_attribute_def_by_id(attr_id)
        if not attr_def:
            raise LookupError(ERR_ATTRIBUTE_DEF_NOT_FOUND)
        self.repository.update_attribute_def_to_common(attr_def)
        return {"ok": True}

    @transaction.atomic
    def make_attribute_specific(self, attr_id: str) -> dict:
        """
        Convert a common AttributeDef to variant-specific (is_common=False).
        Raises LookupError if not found.
        Raises ValueError if already specific.
        """
        attr_def = self.repository.get_attribute_def_by_id(attr_id)
        if not attr_def:
            raise LookupError(ERR_ATTRIBUTE_DEF_NOT_FOUND)
        if not attr_def.is_common:
            raise ValueError(ERR_ALREADY_SPECIFIC)
        self.repository.update_attribute_def_to_specific(attr_def)
        return {"ok": True}

    @transaction.atomic
    def delete_attribute_def(self, attr_id: str) -> dict:
        """
        Delete an AttributeDef by ID.
        Raises LookupError if not found.
        """
        attr_def = self.repository.get_attribute_def_by_id(attr_id)
        if not attr_def:
            raise LookupError(ERR_ATTRIBUTE_DEF_NOT_FOUND)
        self.repository.delete_attribute_def(attr_def)
        return {"ok": True}

    def get_attributes_by_variant(self, variant_key: str, scope: str = "") -> dict:
        """
        Return the full attribute breakdown for a given variant_key:
        - common_attrs: is_common=True, variant_key=None  — from the direct semantic owner type(s)
          (e.g. sdui_container for 'container', sdui_widget for widget variants)
        - catalog_attrs: is_common=False, variant_key=None (with active flag) — from the catalog type(s)
          (e.g. sdui_props)
        - variant_attrs: variant_key=variant_key — from the node type(s) that own this variant
        
        Uses NodeTypeVariant.props_node_type to determine which type holds the catalog.
        """
        scope_type_ids = []
        target_names = None
        if scope:
            scope_type_ids = self.node_type_repository.get_node_type_ids_for_scope(scope)
            target_names = self.repository.get_node_type_names_for_ids(scope_type_ids)

        # All node_type_ids that carry this variant_key (e.g. sdui_props + sdui_widget for 'text_label')
        # If scope is provided, filter by target_names; otherwise get all variant types
        variant_node_type_ids = self.repository.get_node_type_ids_for_variant(variant_key, target_names)

        # Get NodeTypeVariant configs to determine semantic vs catalog types
        ntv_list = self.node_type_repository.get_node_type_variants_by_node_type_ids_and_variant_key(
            variant_node_type_ids,
            variant_key
        )

        # Separate into:
        # - semantic_type_ids: types where the variant lives (sdui_widget, sdui_container, survey_question, field)
        # - catalog_type_ids: types where the props live (sdui_props, or the semantic type itself if props_node_type=None)
        semantic_type_ids = []
        catalog_type_ids = []
        for ntv in ntv_list:
            semantic_type_ids.append(ntv.node_type_id)
            if ntv.props_node_type_id:
                catalog_type_ids.append(ntv.props_node_type_id)
            else:
                catalog_type_ids.append(ntv.node_type_id)

        # Remove duplicates
        semantic_type_ids = list(set(semantic_type_ids))
        catalog_type_ids = list(set(catalog_type_ids))

        # If no semantic type found (e.g. only sdui_props in scope), look one level up
        if not semantic_type_ids:
            semantic_type_ids = self.repository.get_parent_node_type_ids(catalog_type_ids, scope_type_ids)

        # catalog source: prefer dedicated catalog types; fall back to all variant types.
        # Props node types are already included via NodeTypeVariant.props_node_type_id
        # (select_related'd, so out-of-scope props types are still resolved).
        catalog_source_ids = catalog_type_ids if catalog_type_ids else variant_node_type_ids

        common_attrs = self.repository.get_common_attribute_defs(semantic_type_ids)
        for a in common_attrs:
            a["id"] = str(a["id"])

        catalog_attrs = self.repository.get_catalog_attribute_defs_list(catalog_source_ids)
        for a in catalog_attrs:
            a["id"] = str(a["id"])

        variant_attrs = self.repository.get_variant_attribute_defs(variant_node_type_ids, variant_key)
        for a in variant_attrs:
            a["id"] = str(a["id"])

        assigned_json_keys = {a["json_key"] for a in variant_attrs}
        for a in catalog_attrs:
            a["active"] = a["json_key"] in assigned_json_keys

        return {
            "common_attrs": common_attrs,
            "catalog_attrs": catalog_attrs,
            "variant_attrs": variant_attrs,
            "variant_key": variant_key,
        }

    @transaction.atomic
    def save_attributes_by_variant(self, variant_key: str, selected_ids: list, scope: str = "") -> dict:
        """
        Sync the set of catalog AttributeDefs assigned to a variant_key.
        Deletes those removed, creates copies of those added.

        Raises ValueError when a selected catalog template would
        materialize an inert variant-scoped Node-column carrier:

        - a template carrying a column-mapped datatype
          ('display_order' or any 'natural_*'): the copy below
          propagates data_type verbatim, so it would become a
          variant-scoped column carrier — inert, since the field map
          resolves every column carrier at variant_key IS NULL scope
          only. 'natural_version' (AI-117) keeps its specific literal;
          the rest name the datatype (AI-154). Mirrors the
          catalog-import rejection.
        - a template whose name matches a Node column
          (key/name/sort_order/version): the copy propagates name
          verbatim, so it would name-match a Node column under a
          variant — equally inert, since the field map resolves every
          column carrier at IS NULL scope only (post-AI-141). Mirrors
          the catalog-import rejection of the same declaration shape.

        In the structural path the materialized def would never write
        the column nor persist a NodeAttribute row, so the sync is
        rejected whole instead of applying a partial selection
        silently.

        Column-carrier surface verdicts (verified; this and
        create_attribute_def are the rejecting gates — the other
        carriers' channels skip instead):
        - This method is the only admin vector that propagates a
          template's data_type into a variant-scoped def — gated on
          both axes (datatype and name).
        - create_attribute_def can declare a variant-scoped def named
          after a Node column with datatype 'string' (its datatype is
          hardwired via get_default_data_type(), so it can never
          produce a column-mapped datatype) — gated there (AI-141).
        - make_attribute_specific is a non-vector: it only flips
          is_common and keeps variant_key=None (a common def becomes a
          catalog template, never a variant def). A carrier template
          produced that way is rejected here if ever selected.
        - SchemaService materializations (_create_attribute_def,
          _set_node_attribute) can inherit a column-mapped datatype from
          a variant_key=None declaration, but the field-map interceptor
          skips variant-scoped defs and the column-mapped guard never
          persists a NodeAttribute — the def stays inert and the path
          is near-unreachable. Documented, not gated.
        - save_component_properties was the remaining mint vector under
          the service: it created variant-scoped defs through the
          repository when a component property value collided with a
          Node column (e.g. a 'version' property). Now gated there
          (AI-147): the prop is rejected with
          ERR_VARIANT_NODE_FIELD_CARRIER_MSG before the create.
        - NodeService.update_node_properties was a separate real
          vector: an active variant-scoped 'natural_version' def did
          write node.version in the editor. Gated there (AI-132 for
          natural_version, AI-146 for natural_key / natural_order /
          display_order): every column-mapped write branch skips defs
          with a variant_key set, the same variant_key IS NULL rule as
          the import field map — editor and import now agree.
        """
        target_names = None
        if scope:
            type_ids = self.node_type_repository.get_node_type_ids_for_scope(scope)
            target_names = self.repository.get_node_type_names_for_ids(type_ids)

        node_type_ids = self.repository.get_node_type_ids_for_variant(variant_key, target_names)
        selected = set(str(i) for i in selected_ids)
        current = self.repository.get_variant_attr_ids(node_type_ids, variant_key)
        current_str = set(str(i) for i in current)

        to_delete = current_str - selected
        if to_delete:
            self.repository.delete_attribute_defs_by_ids(to_delete)

        to_create_ids = selected - current_str
        for attr_id in to_create_ids:
            catalog_attr = self.repository.get_catalog_attribute_def_by_id(node_type_ids, attr_id)
            if catalog_attr:
                # Reject column-mapped datatype templates: the copy
                # would be a variant-scoped column carrier, which is
                # inert (see docstring). The natural key reported is the
                # template's own (node_type, json_key, variant_key=None);
                # the variant_key in the message is the assignment
                # target. 'natural_version' keeps its specific literal;
                # the rest name the datatype.
                template_dt = catalog_attr.data_type.name
                if is_column_mapped_datatype(template_dt):
                    if template_dt == 'natural_version':
                        raise ValueError(
                            ERR_VARIANT_VERSION_CARRIER_MSG.format(
                                natural_key=[
                                    catalog_attr.node_type.name,
                                    catalog_attr.json_key,
                                    None,
                                ],
                                variant_key=variant_key,
                            )
                        )
                    raise ValueError(
                        ERR_VARIANT_COLUMN_CARRIER_MSG.format(
                            natural_key=[
                                catalog_attr.node_type.name,
                                catalog_attr.json_key,
                                None,
                            ],
                            data_type=template_dt,
                            variant_key=variant_key,
                        )
                    )
                # Same rule on the name axis: a template named after a
                # Node column would copy into a variant-scoped def that
                # still name-matches the column — inert in the field
                # map (see docstring), so the assignment is rejected.
                if catalog_attr.name in _AUTHOR_SETTABLE_NODE_FIELDS:
                    raise ValueError(
                        ERR_VARIANT_NODE_FIELD_CARRIER_MSG.format(
                            natural_key=[
                                catalog_attr.node_type.name,
                                catalog_attr.json_key,
                                None,
                            ],
                            name=catalog_attr.name,
                            variant_key=variant_key,
                        )
                    )
                self.repository.create_attribute_def(
                    node_type=catalog_attr.node_type,
                    name=catalog_attr.name,
                    json_key=catalog_attr.json_key,
                    data_type=catalog_attr.data_type,
                    is_required=catalog_attr.is_required,
                    is_common=False,
                    variant_key=variant_key,
                    domain=catalog_attr.domain,
                    group=catalog_attr.group,
                )

        return {"ok": True}
