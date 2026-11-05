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


class CanProposeChangeset(UserMCPTokenPermission):
    """Level-2 permission plus the per-token changeset-propose flag.

    Gates governed catalog mutation: proposing a changeset is a weaker
    grant than direct catalog authoring (the ops still pass validation and
    human review), so it carries its own capability flag on top of
    ``min_level = 2``. Same duck-typed closure as the base class — project
    credentials carry neither ``mcp_level`` nor ``can_propose_changeset``
    on ``request.auth`` and fail closed.

    The ``allowed_scopes`` half of the capability is intentionally NOT
    enforced here: a changeset's ``scopes`` are derived server-side during
    validation, so the intersection check runs per-endpoint (see
    :meth:`covers_scopes`) once those scopes exist — not on the raw
    request payload.
    """

    min_level = 2

    def has_permission(self, request, view) -> bool:
        return bool(
            super().has_permission(request, view)
            and getattr(request.auth, 'can_propose_changeset', False)
        )

    @staticmethod
    def covers_scopes(auth, scopes) -> bool:
        """Whether the token row in ``auth`` may touch every derived scope.

        ``allowed_scopes`` NULL or an empty list means unrestricted — the
        only bound a user token can carry, since the catalog grammar is
        global. A malformed (non-list) stored value fails closed —
        including falsy non-lists like ``""``, ``0``, ``False``, ``{}``,
        which the JSONField will happily store. ``scopes`` is the
        changeset's derived scope list; an empty/None changeset scope set
        is always covered. Non-scalar elements anywhere fail closed.
        """
        allowed = getattr(auth, 'allowed_scopes', None)
        if allowed is None:
            return True
        if not isinstance(allowed, (list, tuple, set, frozenset)):
            return False
        if not allowed:
            return True
        if not isinstance(scopes, (list, tuple, set, frozenset)):
            return False if scopes else True
        try:
            return set(scopes or ()) <= set(allowed)
        except TypeError:
            return False
