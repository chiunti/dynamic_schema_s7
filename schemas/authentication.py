"""Machine authentication for schema-domain endpoints.

Two bearer credential types coexist: project credentials (B2B, resolved to a
``ProjectPrincipal``) and per-user MCP tokens (resolved to the real user).
"""

from dataclasses import dataclass
from uuid import UUID

from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import BasePermission

from .constants import ERR_INVALID_USER_MCP_TOKEN
from .services.project_credential_service import ProjectCredentialService
from .services.user_mcp_token_service import UserMCPTokenService


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


class UserMCPTokenAuthentication(BaseAuthentication):
    """Bearer auth for per-user MCP tokens; soft-fails so project auth can run next."""

    def authenticate(self, request):
        parts = get_authorization_header(request).split()
        if not parts or parts[0].lower() != b'bearer':
            return None
        if len(parts) != 2:
            raise AuthenticationFailed(ERR_INVALID_USER_MCP_TOKEN)
        try:
            token = parts[1].decode('ascii')
        except UnicodeDecodeError as exc:
            raise AuthenticationFailed(ERR_INVALID_USER_MCP_TOKEN) from exc
        token_row = UserMCPTokenService().authenticate(token)
        if token_row is None:
            return None  # not ours (or dead) — let the next authenticator decide
        return token_row.user, token_row

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


class UserMCPTokenPermission(BasePermission):
    """Grant schema-domain access to user MCP tokens at or above ``min_level``.

    Duck-typed on ``request.auth.mcp_level``: a ``ProjectAPICredential``
    carries no such attribute, so project principals never satisfy this
    permission — they rely on their scope permissions instead.
    """

    min_level = 1

    def has_permission(self, request, view) -> bool:
        return bool(
            request.user and request.user.is_authenticated
            and getattr(request.auth, 'mcp_level', 0) >= self.min_level
        )


class CanAuthorCatalog(UserMCPTokenPermission):
    """MCP level-2 permission for catalog grammar authoring.

    Gates the catalog import endpoint where user MCP tokens write grammar
    objects (node types, attribute defs, compositions, domains, variants).
    Project credentials can never satisfy it: their ``request.auth`` carries
    no ``mcp_level`` attribute, so the duck-typed level check fails closed.
    """

    min_level = 2
