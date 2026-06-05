"""
HTTP view functions for the component properties admin API.
These views are thin wrappers that delegate all business logic to AttributeDefService.
"""


from django.http import HttpRequest, JsonResponse

from .services.attribute_def_service import AttributeDefService
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
    if request.method == "GET":
        result = AttributeDefService().get_attributes_by_variant(variant_key, scope)
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
