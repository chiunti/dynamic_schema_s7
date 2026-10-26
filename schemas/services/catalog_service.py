"""Resolved catalog service for machine consumers (AI agents).

Builds the denormalized per-scope catalog — one entry per reachable node
type carrying its attribute definitions, digested NodeTypeVariant metadata
and compositions grouped by collection_key — plus the referenced domain
dictionaries, the registered grammar docs for the non-trivial datatypes in
use and an embedded JSON example. Consumers can author valid JSON without
replicating the internal variant/composition resolution logic.
"""

from copy import deepcopy
from typing import Any, Optional

from ..repositories.attribute_def_repository import AttributeDefRepository
from ..repositories.composition_repository import CompositionRepository
from ..repositories.data_type_repository import DataTypeRepository
from ..repositories.node_type_repository import NodeTypeRepository
from .datatype_plugins import get_datatype_doc, is_server_managed_attribute
from .exceptions import ScopeNotFoundError
from .schema_service import SchemaService


class CatalogService:
    """Service that assembles the resolved catalog payloads for API consumers."""

    def __init__(self) -> None:
        self.attribute_def_repository = AttributeDefRepository()
        self.node_type_repository = NodeTypeRepository()
        self.composition_repository = CompositionRepository()
        self.data_type_repository = DataTypeRepository()
        self.schema_service = SchemaService()

    def get_datatypes(self) -> dict:
        """Return the universal DataType catalog.

        Returns:
            Dict with a "datatypes" list of {name, description,
            primary_storage_type} entries ordered by name.
        """
        return {"datatypes": self.data_type_repository.list_data_types()}

    def list_root_scopes(self) -> list[str]:
        """Return the json_scope values declared by root NodeTypes.

        Returns:
            Sorted list of non-null json_scope strings.
        """
        return self.node_type_repository.list_root_scopes()

    def get_resolved_catalog(self, scope: str) -> dict:
        """Return the resolved catalog for a root json_scope.

        Args:
            scope: json_scope of the root NodeType to resolve.

        Returns:
            Dict with keys:
            - scope: the requested json_scope
            - root_node_type: name of the scope's root NodeType
            - node_types: denormalized entries ordered by name, each with
              attributes (ordered by json_key; every attribute entry carries
              ``server_generated`` — True for engine-managed ``natural_*``
              attributes such as ``natural_uuid`` bound to ``id`` and for
              ``display_order`` defs, whose value derives from
              ``node.sort_order``; the flag also forces ``required`` to
              False), a resolved variants block
              (None when the type declares none) and compositions grouped
              by collection_key (groups ordered by key, structural
              None group last)
            - domains: {domain_name: [{value, label}]} for every domain
              referenced by the scope's attribute definitions
            - datatypes: {datatype_name: doc} for every datatype used by the
              scope that has a DATATYPE_DOCS entry (e.g. the conditional
              grammar); always present — {} when the scope uses no
              documented datatype
            - example: embedded JSON example for the scope, or None

        Raises:
            ScopeNotFoundError: a LookupError subclass carrying the valid
            root scopes in ``available_scopes`` when no root NodeType
            matches the given scope.
        """
        root = self.node_type_repository.get_root_node_type_by_scope(scope)
        if root is None:
            raise ScopeNotFoundError(self.list_root_scopes())

        type_ids = self.node_type_repository.get_node_type_ids_for_root(root.id)
        node_types = list(self.node_type_repository.get_node_types_by_ids(type_ids))
        variant_rows = self.node_type_repository.get_node_type_variants_by_node_type_ids(type_ids)
        attr_rows = self.attribute_def_repository.get_attribute_defs_by_node_type_ids(type_ids)
        compositions = list(
            self.composition_repository.get_compositions_with_child_types_by_parent_ids(type_ids)
        )

        attrs_by_type = self._index_by(attr_rows, "node_type_id")
        variants_by_type = self._index_by(variant_rows, "node_type_id")
        comps_by_type: dict[Any, list] = {}
        for comp in compositions:
            comps_by_type.setdefault(comp.parent_type_id, []).append(comp)

        catalog_types = []
        for node_type in node_types:
            type_id = node_type["id"]
            attributes = []
            for row in sorted(attrs_by_type.get(type_id, []), key=lambda r: r["json_key"]):
                server_managed = is_server_managed_attribute(
                    row["data_type__name"], row["name"]
                )
                attributes.append(
                    {
                        "json_key": row["json_key"],
                        "name": row["name"],
                        "data_type": row["data_type__name"],
                        "domain": row["domain__domain_name"],
                        "required": False if server_managed else row["is_required"],
                        "server_generated": server_managed,
                        "common": row["is_common"],
                        "variant_key": row["variant_key"],
                    }
                )
            catalog_types.append(
                {
                    "name": node_type["name"],
                    "label": node_type["label"],
                    "is_root": node_type["is_root"],
                    "is_container": node_type["is_container"],
                    "default_json_key": node_type["default_json_key"],
                    "is_json_map": node_type["is_json_map"],
                    "json_scalar_attribute": node_type["json_scalar_attribute"],
                    "attributes": attributes,
                    "variants": self._build_variants(variants_by_type.get(type_id, [])),
                    "compositions": self._build_compositions(comps_by_type.get(type_id, [])),
                }
            )

        return {
            "scope": scope,
            "root_node_type": root.name,
            "node_types": catalog_types,
            "domains": self._build_domains(attr_rows),
            "datatypes": self._build_datatype_docs(attr_rows),
            "example": self.schema_service.generate_json_example_for_root(root),
        }

    @staticmethod
    def _index_by(rows: list[dict], key: str) -> dict[Any, list[dict]]:
        """Group dict rows by the value of a field, preserving row order."""
        grouped: dict[Any, list[dict]] = {}
        for row in rows:
            grouped.setdefault(row[key], []).append(row)
        return grouped

    def _build_variants(self, variant_rows: list[dict]) -> Optional[dict]:
        """Digest a node type's NodeTypeVariant rows into a variant block.

        Returns None when the type declares no variants. discriminator_attr
        and props_node_type take the first non-null value across the type's
        rows; keys lists every declared variant_key sorted.
        """
        if not variant_rows:
            return None
        return {
            "discriminator_attr": next(
                (row["discriminator_attr"] for row in variant_rows if row["discriminator_attr"]),
                None,
            ),
            "props_node_type": next(
                (row["props_node_type__name"] for row in variant_rows if row["props_node_type__name"]),
                None,
            ),
            "keys": sorted({row["variant_key"] for row in variant_rows}),
        }

    def _build_compositions(self, composition_rows: list) -> list[dict]:
        """Group a node type's composition rows by collection_key.

        composition_rows are NodeTypeComposition instances with child_type
        already select_related. The emitted group order is explicit contract:
        groups sorted by collection_key, with the None group (structural
        children, whose JSON key comes from each child's default_json_key)
        always last — independent of the repository's row ordering.
        """
        grouped: dict[Optional[str], list[dict]] = {}
        for comp in composition_rows:
            grouped.setdefault(comp.collection_key, []).append(
                {
                    "name": comp.child_type.name,
                    "default_json_key": comp.child_type.default_json_key,
                    "min_children": comp.min_children,
                    "max_children": comp.max_children,
                }
            )
        return [
            {"collection_key": collection_key, "child_types": child_types}
            for collection_key, child_types in sorted(
                grouped.items(), key=lambda item: (item[0] is None, item[0])
            )
        ]

    def _build_domains(self, attr_rows: list[dict]) -> dict:
        """Resolve the items of every domain referenced by attribute rows.

        Returns {domain_name: [{value, label}, ...]}; referenced domains
        with no items still appear with an empty list.
        """
        domain_names = sorted(
            {row["domain__domain_name"] for row in attr_rows if row["domain__domain_name"]}
        )
        domains: dict[str, list[dict]] = {name: [] for name in domain_names}
        for item in self.attribute_def_repository.get_domain_items_by_domain_names(domain_names):
            domains[item["domain__domain_name"]].append(
                {"value": item["value"], "label": item["label"]}
            )
        return domains

    def _build_datatype_docs(self, attr_rows: list[dict]) -> dict:
        """Collect the registered grammar docs for datatypes used by the scope.

        Returns {datatype_name: doc} for every data_type present in attr_rows
        that has a DATATYPE_DOCS entry, ordered by name for deterministic
        output. Undocumented datatypes (plain scalars) are skipped. The
        section is always emitted — {} when the scope uses no documented
        datatype — so consumers can rely on the key existing.
        """
        docs: dict[str, dict] = {}
        for name in sorted({row["data_type__name"] for row in attr_rows}):
            doc = get_datatype_doc(name)
            if doc is not None:
                docs[name] = deepcopy(doc)
        return docs
