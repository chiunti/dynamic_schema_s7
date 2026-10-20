"""Lifecycle and validation of per-user MCP tokens."""

import hashlib
import secrets
import uuid
from datetime import date
from typing import TYPE_CHECKING, Optional

from django.db import transaction
from django.utils import timezone

from ..models import UserMCPToken
from ..repositories.user_repository import UserMCPTokenRepository, UserRepository

if TYPE_CHECKING:
    from ..repositories.user_repository import User


class UserMCPTokenService:
    """Provisioning, authentication and revocation of per-user MCP tokens."""

    LEVELS = frozenset(UserMCPToken.Level.values)

    def __init__(self) -> None:
        self.tokens = UserMCPTokenRepository()
        self.users = UserRepository()

    def get_user_by_email(self, email: str) -> Optional['User']:
        """Resolve a user row for provisioning entry points."""
        return self.users.get_user_by_email(email)

    @transaction.atomic
    def provision(self, user, name: str, token: str, mcp_level: int,
                  expires_at: date) -> 'UserMCPToken':
        if not name.strip() or mcp_level not in self.LEVELS:
            raise ValueError('A name and a valid MCP level (1 or 2) are required')
        if not token or len(token) < 43:
            raise ValueError('A high-entropy token is required')
        if expires_at < timezone.localdate():
            raise ValueError('Token expiry must not be in the past')
        if user is None or not user.is_active:
            raise ValueError('An active user is required')
        digest = hashlib.sha256(token.encode('utf-8')).hexdigest()
        return self.tokens.create(user, name.strip(), digest, mcp_level, expires_at)

    def authenticate(self, token: str) -> Optional['UserMCPToken']:
        if not token or len(token) < 43:
            return None
        digest = hashlib.sha256(token.encode('utf-8')).hexdigest()
        mcp_token = self.tokens.get_by_digest(digest)
        if (mcp_token is None or not secrets.compare_digest(mcp_token.token_digest, digest)
                or mcp_token.revoked_at is not None or mcp_token.expires_at < timezone.localdate()
                or not mcp_token.user.is_active):
            return None
        return mcp_token

    @transaction.atomic
    def revoke(self, token_id: uuid.UUID) -> bool:
        return self.tokens.revoke(token_id, timezone.now())

    def is_deletable(self, token: 'UserMCPToken') -> bool:
        """Business rule: only revoked or expired tokens may be deleted."""
        return bool(token.revoked_at) or token.expires_at < timezone.localdate()
