"""
Utility functions for the schemas app.
"""

import functools
import json
import uuid
from typing import Any, Callable, Dict

from django.http import HttpRequest, HttpResponse, JsonResponse

from .models import Organization, Project, OrganizationMember


def api_login_required(
    view_func: Callable[..., HttpResponse],
) -> Callable[..., HttpResponse]:
    """Require an authenticated user, answering with a JSON 401.

    Drop-in replacement for ``django.contrib.auth.decorators.login_required``
    on JSON API views: anonymous requests get
    ``{"error": "authentication_required"}`` with status 401 instead of a
    302 redirect to the HTML login page.
    """

    @functools.wraps(view_func)
    def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if not request.user.is_authenticated:
            from .constants import ERR_AUTHENTICATION_REQUIRED

            return JsonResponse({"error": ERR_AUTHENTICATION_REQUIRED}, status=401)
        return view_func(request, *args, **kwargs)

    return wrapper


def parse_uuid(value: str, field: str) -> uuid.UUID:
    """Parse a UUID string and return a UUID object.
    
    Args:
        value: String representation of UUID
        field: Field name for error messages
        
    Returns:
        UUID object
        
    Raises:
        ValueError: If value is not a valid UUID
    """
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError):
        from .constants import ERR_INVALID_UUID
        raise ValueError(ERR_INVALID_UUID.format(field=field, value=value))


def json_body(request) -> dict:
    """Parse JSON body from HTTP request.

    Args:
        request: HTTP request object

    Returns:
        Parsed JSON as dictionary

    Raises:
        ValueError: If body is not valid JSON or does not parse to an object
    """
    try:
        parsed = json.loads(request.body.decode("utf-8")) if request.body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        from .constants import ERR_INVALID_JSON_BODY
        raise ValueError(ERR_INVALID_JSON_BODY)
    if not isinstance(parsed, dict):
        from .constants import ERR_JSON_BODY_MUST_BE_OBJECT
        raise ValueError(ERR_JSON_BODY_MUST_BE_OBJECT)
    return parsed


def org_to_dict(org: Organization) -> Dict[str, Any]:
    """Convert Organization model to dictionary.
    
    Args:
        org: Organization model instance
        
    Returns:
        Dictionary representation of organization
    """
    return {
        "id": str(org.id),
        "name": org.name,
        "slug": org.slug,
        "description": org.description,
        "is_active": org.is_active,
        "created_at": org.created_at.isoformat(),
        "updated_at": org.updated_at.isoformat(),
    }


def project_to_dict(project: Project) -> Dict[str, Any]:
    """Convert Project model to dictionary.
    
    Args:
        project: Project model instance
        
    Returns:
        Dictionary representation of project
    """
    return {
        "id": str(project.id),
        "name": project.name,
        "slug": project.slug,
        "description": project.description,
        "organization_id": str(project.organization_id) if project.organization_id else None,
        "organization_name": project.organization.name if project.organization_id else None,
        "created_by": str(project.created_by_id) if project.created_by_id else None,
        "created_at": project.created_at.isoformat(),
        "updated_at": project.updated_at.isoformat(),
    }


def member_to_dict(member: OrganizationMember) -> Dict[str, Any]:
    """Convert OrganizationMember model to dictionary.

    Args:
        member: OrganizationMember model instance

    Returns:
        Dictionary representation of member
    """
    return {
        "id": str(member.id),
        "user_id": str(member.user_id),
        "user_email": member.user.email,
        "role": member.role,
        "joined_at": member.joined_at.isoformat(),
    }
