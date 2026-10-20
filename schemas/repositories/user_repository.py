"""
Repository for User data access.
"""

import uuid
from typing import Optional

from django.contrib.auth import get_user_model

from ..models import UserMCPToken

User = get_user_model()


class UserRepository:
    """Repository for user-related database operations."""

    def get_user_by_email(self, email: str) -> Optional[User]:
        """
        Get user by email address.

        Args:
            email: Email address to search for

        Returns:
            User instance or None if not found
        """
        return User.objects.filter(email=email).first()


class UserMCPTokenRepository:
    """Persistence for per-user MCP tokens."""

    def get_by_digest(self, digest: str) -> Optional[UserMCPToken]:
        return UserMCPToken.objects.select_related("user").filter(token_digest=digest).first()

    def create(self, user, name: str, digest: str, mcp_level: int, expires_at) -> UserMCPToken:
        return UserMCPToken.objects.create(
            user=user, name=name, token_digest=digest,
            mcp_level=mcp_level, expires_at=expires_at,
        )

    def revoke(self, token_id: uuid.UUID, revoked_at) -> bool:
        return bool(
            UserMCPToken.objects.filter(id=token_id, revoked_at__isnull=True)
            .update(revoked_at=revoked_at)
        )
