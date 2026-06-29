"""
HTTP view functions for the component properties admin API.
These views are thin wrappers that delegate all business logic to AttributeDefService.
"""


import json
from django.http import HttpRequest, JsonResponse

from .services.attribute_def_service import AttributeDefService
from .repositories.node_type_repository import NodeTypeRepository
from .repositories.attribute_def_repository import AttributeDefRepository
from .repositories.composition_repository import CompositionRepository
from .constants import (
    ERR_METHOD_NOT_ALLOWED,
    ERR_INVALID_JSON,
    ERR_VARIANT_KEY_NOT_FOUND,
    ERR_DEFAULT_DATA_TYPE_NOT_FOUND,
    ERR_JSON_KEY_DUPLICATE,
    ERR_ID_REQUIRED,
)


def api_component_types(request: HttpRequest) -> JsonResponse:
    if request.method != "GET":
        return JsonResponse({"error": ERR_METHOD_NOT_ALLOWED}, status=405)
    scope = request.GET.get("scope", "")
    try:
        result = AttributeDefService().get_component_types_for_scope(scope)
        return JsonResponse(result)
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=400)
    except LookupError as e:
        return JsonResponse({"error": str(e)}, status=404)


def api_component_properties(request: HttpRequest, component_type: str) -> JsonResponse:
    if request.method != "GET":
        return JsonResponse({"error": ERR_METHOD_NOT_ALLOWED}, status=405)
    result = AttributeDefService().get_component_properties(component_type)
    return JsonResponse(result)


def api_save_component_properties(request: HttpRequest, component_type: str) -> JsonResponse:
    if request.method != "POST":
        return JsonResponse({"error": ERR_METHOD_NOT_ALLOWED}, status=405)
    try:
        payload = json.loads(request.body.decode("utf-8")) if request.body else {}
    except json.JSONDecodeError:
        return JsonResponse({"error": ERR_INVALID_JSON}, status=400)
    try:
        result = AttributeDefService().save_component_properties(
            component_type, payload.get("properties", [])
        )
        return JsonResponse(result)
    except TypeError as e:
        return JsonResponse({"error": str(e)}, status=400)
    except LookupError as e:
        return JsonResponse({"error": str(e)}, status=500)


def api_variants_by_scope(request: HttpRequest) -> JsonResponse:
    if request.method != "GET":
        return JsonResponse({"error": ERR_METHOD_NOT_ALLOWED}, status=405)
    scope = request.GET.get("scope", "")
    result = AttributeDefService().get_variants_for_scope(scope)
    return JsonResponse(result)


def api_create_attribute_def(request: HttpRequest) -> JsonResponse:
    if request.method != "POST":
        return JsonResponse({"error": ERR_METHOD_NOT_ALLOWED}, status=405)
    try:
        payload = json.loads(request.body.decode("utf-8")) if request.body else {}
    except json.JSONDecodeError:
        return JsonResponse({"error": ERR_INVALID_JSON}, status=400)
    try:
        result = AttributeDefService().create_attribute_def(
            variant_key=payload.get("variant_key"),
            json_key=payload.get("json_key"),
            name=payload.get("name"),
            is_required=payload.get("is_required", False),
            is_common=payload.get("is_common", False),
            add_to_catalog=payload.get("add_to_catalog", False),
        )
        return JsonResponse(result)
    except ValueError as e:
        status_code = 409 if str(e) == ERR_JSON_KEY_DUPLICATE else 400
        return JsonResponse({"error": str(e)}, status=status_code)
    except LookupError as e:
        status_code = 404 if str(e) in (ERR_VARIANT_KEY_NOT_FOUND, ERR_DEFAULT_DATA_TYPE_NOT_FOUND) else 500
        return JsonResponse({"error": str(e)}, status=status_code)


def api_update_attribute_common(request: HttpRequest) -> JsonResponse:
    if request.method != "POST":
        return JsonResponse({"error": ERR_METHOD_NOT_ALLOWED}, status=405)
    try:
        payload = json.loads(request.body.decode("utf-8")) if request.body else {}
    except json.JSONDecodeError:
        return JsonResponse({"error": ERR_INVALID_JSON}, status=400)
    attr_id = payload.get("id")
    if not attr_id:
        return JsonResponse({"error": ERR_ID_REQUIRED}, status=400)
    try:
        result = AttributeDefService().make_attribute_common(attr_id)
        return JsonResponse(result)
    except LookupError as e:
        return JsonResponse({"error": str(e)}, status=404)


def api_update_attribute_specific(request: HttpRequest) -> JsonResponse:
    if request.method != "POST":
        return JsonResponse({"error": ERR_METHOD_NOT_ALLOWED}, status=405)
    try:
        payload = json.loads(request.body.decode("utf-8")) if request.body else {}
    except json.JSONDecodeError:
        return JsonResponse({"error": ERR_INVALID_JSON}, status=400)
    attr_id = payload.get("id")
    if not attr_id:
        return JsonResponse({"error": ERR_ID_REQUIRED}, status=400)
    try:
        result = AttributeDefService().make_attribute_specific(attr_id)
        return JsonResponse(result)
    except LookupError as e:
        return JsonResponse({"error": str(e)}, status=404)
    except ValueError as e:
        return JsonResponse({"error": str(e)}, status=400)


def api_delete_attribute_def(request: HttpRequest) -> JsonResponse:
    if request.method != "POST":
        return JsonResponse({"error": ERR_METHOD_NOT_ALLOWED}, status=405)
    try:
        payload = json.loads(request.body.decode("utf-8")) if request.body else {}
    except json.JSONDecodeError:
        return JsonResponse({"error": ERR_INVALID_JSON}, status=400)
    attr_id = payload.get("id")
    if not attr_id:
        return JsonResponse({"error": ERR_ID_REQUIRED}, status=400)
    try:
        result = AttributeDefService().delete_attribute_def(attr_id)
        return JsonResponse(result)
    except LookupError as e:
        return JsonResponse({"error": str(e)}, status=404)


def api_attributes_by_variant(request: HttpRequest, variant_key: str) -> JsonResponse:
    scope = request.GET.get("scope", "")
    parent_node_id = request.GET.get("parent_node_id")
    
    if request.method == "GET":
        # If parent_node_id is provided, use it to infer variant_key for props nodes
        # This allows the component properties editor to work when editing props child nodes
        effective_variant_key = variant_key
        if parent_node_id:
            from .services.node_service import NodeService
            from .repositories.schema_repository import SchemaRepository
            
            node_service = NodeService()
            schema_repo = SchemaRepository()
            
            # Get the parent node
            parent = schema_repo.get_node_by_id_with_node_type(parent_node_id)
            if parent:
                # Use NodeService to infer variant from parent
                effective_variant_key = node_service.infer_variant_from_parent(parent)
        
        if not effective_variant_key:
            return JsonResponse({"error": "variant_key is required or could not be inferred from parent_node_id"}, status=400)
        
        result = AttributeDefService().get_attributes_by_variant(effective_variant_key, scope)
        return JsonResponse(result)

    if request.method == "POST":
        try:
            payload = json.loads(request.body.decode("utf-8")) if request.body else {}
        except json.JSONDecodeError:
            return JsonResponse({"error": ERR_INVALID_JSON}, status=400)
        result = AttributeDefService().save_attributes_by_variant(
            variant_key, payload.get("selected_ids", []), scope
        )
        return JsonResponse(result)

    return JsonResponse({"error": ERR_METHOD_NOT_ALLOWED}, status=405)


def api_json_example(request: HttpRequest) -> JsonResponse:
    """Generate JSON example dynamically from DB definitions"""
    if request.method != "GET":
        return JsonResponse({"error": ERR_METHOD_NOT_ALLOWED}, status=405)
    
    schema_type = request.GET.get('schema_type')
    
    if not schema_type:
        return JsonResponse({"error": "schema_type parameter required"}, status=400)
    
    root_node_type = NodeTypeRepository().get_root_node_type_by_scope(schema_type)
    if not root_node_type:
        return JsonResponse({"error": "Invalid schema type"}, status=400)
    
    # Generate example based on NodeType and compositions
    example = _generate_json_example(root_node_type)
    
    return JsonResponse({"example": example})


def _generate_json_example(node_type):
    """Generate JSON example from NodeType definitions (only root level with required fields)"""
    attr_def_repo = AttributeDefRepository()
    comp_repo = CompositionRepository()
    from schemas.models import NodeTypeComposition

    example = {}

    # Start with the root key (node_type name or json_scope without _root suffix)
    root_key = node_type.json_scope.replace('_root', '') if node_type.json_scope else node_type.name
    example[root_key] = {}

    # Add only required attributes for this node type
    attr_defs = attr_def_repo.get_attribute_defs_by_node_type_required(node_type, variant_key=None)
    for attr_def in attr_defs:
        if attr_def.json_key in ['id', 'key', 'version']:
            continue  # Skip auto-generated fields
        example[root_key][attr_def.json_key] = _get_example_value(attr_def)

    # Add empty array placeholders for collections (only structure, no content)
    compositions = comp_repo.get_compositions_by_node_type(node_type)
    for composition in compositions:
        if composition.collection_key:
            example[root_key][composition.collection_key] = []

    return example


def _get_example_value(attr_def):
    """Generate example value based on DataType"""
    from schemas.models import DataType
    
    data_type_name = attr_def.data_type.name if attr_def.data_type else 'string'
    
    if data_type_name == 'string':
        return f"example_{attr_def.json_key}"
    elif data_type_name == 'int':
        return 1
    elif data_type_name == 'number':
        return 1.0
    elif data_type_name == 'bool':
        return True
    elif data_type_name == 'list_string':
        return ["option1", "option2"]
    elif data_type_name == 'json':
        return {}
    elif data_type_name == 'date':
        return "2024-01-01"
    else:
        return f"example_{attr_def.json_key}"
