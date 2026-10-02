"""Machine authentication for project-scoped schema endpoints."""

from dataclasses import dataclass
from uuid import UUID

from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import BasePermission

from .services.project_credential_service import ProjectCredentialService


@dataclass(frozen=True)
class ProjectPrincipal:
    project_id: UUID
    organization_id: UUID
    is_authenticated: bool = True


class ProjectTokenAuthentication(BaseAuthentication):
    def authenticate(self, request):
        parts = get_authorization_header(request).split()
        if not parts or parts[0].lower() != b'bearer':
            return None
        if len(parts) != 2:
            raise AuthenticationFailed('Invalid project credential')
        try:
            token = parts[1].decode('ascii')
        except UnicodeDecodeError as exc:
            raise AuthenticationFailed('Invalid project credential') from exc
        credential = ProjectCredentialService().authenticate(token)
        if credential is None:
            raise AuthenticationFailed('Invalid project credential')
        return ProjectPrincipal(credential.project_id, credential.project.organization_id), credential

    def authenticate_header(self, request) -> str:
        return 'Bearer'


class ProjectScopePermission(BasePermission):
    scope: str = ''

    def has_permission(self, request, view) -> bool:
        return isinstance(request.user, ProjectPrincipal) and bool(getattr(request.auth, self.scope, False))


class CanReadSchema(ProjectScopePermission):
    scope = 'can_read'


class CanImportSchema(ProjectScopePermission):
    scope = 'can_import'


class CanPublishSchema(ProjectScopePermission):
    scope = 'can_publish'
