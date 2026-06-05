"""
Business logic for AttributeDef, component type, and variant property management.
"""

from ..models import DomainItem
from ..repositories.attribute_def_repository import AttributeDefRepository
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
)


class AttributeDefService:
    """Service for component property and attribute definition business logic."""

    def __init__(self):
        self.repository = AttributeDefRepository()

    def get_component_types_for_scope(self, scope: str) -> dict:
        """
        Return the list of component types (DomainItems) reachable from a root scope.
        Raises ValueError if scope is missing or not found, or if no type domain exists.
        """
        if not scope:
            raise ValueError(ERR_SCOPE_REQUIRED)

        attr_def = self.repository.get_type_attribute_def_for_scope(scope)
        if not attr_def:
            root = self.repository.get_root_node_type_by_scope(scope)
            if not root:
                raise LookupError(ERR_SCOPE_NOT_FOUND)
            raise LookupError(ERR_COMPONENT_TYPE_DOMAIN_NOT_FOUND)

        component_types = self.repository.get_component_types_for_domain(attr_def.domain)
        return {"component_types": component_types}

    def get_component_properties(self, component_type: str) -> dict:
        """Return the property DomainItems for a given component type."""
        domain_name = f"properties_{component_type}"
        domain = self.repository.get_domain_by_name(domain_name)
        if not domain:
            return {"domain_name": domain_name, "properties": []}
        properties = self.repository.get_domain_items(domain)
        return {"domain_name": domain_name, "properties": properties}

    def save_component_properties(self, component_type: str, properties: list) -> dict:
        """
        Replace the DomainItems for a component type domain and sync variant AttributeDefs.
        Raises TypeError if properties is not a list.
        Raises LookupError if the default DataType is missing.
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
            domain_items_to_create.append(
                DomainItem(domain=domain, value=prop_value, label=label, extra_metadata=None)
            )

        if domain_items_to_create:
            self.repository.bulk_create_domain_items(domain_items_to_create)

        return {
            "ok": True,
            "domain_name": domain_name,
            "properties_count": len(domain_items_to_create),
        }

    def get_variants_for_scope(self, scope: str) -> dict:
        """Return all NodeTypeVariants reachable from a given root scope."""
        variants = self.repository.get_all_variants(scope)
        return {"variants": variants}

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
        """
        if not json_key or not name:
            raise ValueError(ERR_JSON_KEY_AND_NAME_REQUIRED)

        if not self.repository.variant_key_exists(variant_key):
            raise LookupError(ERR_VARIANT_KEY_NOT_FOUND)

        node_type = self.repository.get_node_type_by_variant(variant_key)

        effective_variant_key = variant_key
        if is_common or add_to_catalog:
            effective_variant_key = None

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
        - common_attrs: is_common=True, variant_key=None
        - catalog_attrs: is_common=False, variant_key=None (with active flag)
        - variant_attrs: is_common=False, variant_key=variant_key
        """
        target_names = None
        if scope:
            type_ids = self.repository.get_node_type_ids_for_scope(scope)
            target_names = self.repository.get_node_type_names_for_ids(type_ids)

        node_type_ids = self.repository.get_node_type_ids_for_variant(variant_key, target_names)

        common_attrs = self.repository.get_common_attribute_defs(node_type_ids)
        for a in common_attrs:
            a["id"] = str(a["id"])

        catalog_attrs = self.repository.get_catalog_attribute_defs_list(node_type_ids)
        for a in catalog_attrs:
            a["id"] = str(a["id"])

        variant_attrs = self.repository.get_variant_attribute_defs(node_type_ids, variant_key)
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

    def save_attributes_by_variant(self, variant_key: str, selected_ids: list, scope: str = "") -> dict:
        """
        Sync the set of catalog AttributeDefs assigned to a variant_key.
        Deletes those removed, creates copies of those added.
        """
        target_names = None
        if scope:
            type_ids = self.repository.get_node_type_ids_for_scope(scope)
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
