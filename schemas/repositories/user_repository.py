"""
Repository for User data access.
"""

from typing import Optional
from django.contrib.auth import get_user_model

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
