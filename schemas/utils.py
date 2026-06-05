"""
Utility functions for the schemas app.
"""

import json
import uuid
from typing import Dict, Any

from .models import Organization, Project, OrganizationMember


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
        ValueError: If body is not valid JSON
    """
    try:
        return json.loads(request.body.decode("utf-8")) if request.body else {}
    except json.JSONDecodeError:
        from .constants import ERR_INVALID_JSON_BODY
        raise ValueError(ERR_INVALID_JSON_BODY)


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
