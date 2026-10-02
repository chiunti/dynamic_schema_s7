"""Lifecycle and validation of project service credentials."""

import hashlib
import secrets
import uuid
from datetime import date
from typing import TYPE_CHECKING, Optional

from django.db import transaction
from django.utils import timezone

from ..repositories.project_repository import ProjectCredentialRepository, ProjectRepository

if TYPE_CHECKING:
    from ..models import ProjectAPICredential


class ProjectCredentialService:
    SCOPES = frozenset({'read', 'import', 'publish'})

    def __init__(self) -> None:
        self.credentials = ProjectCredentialRepository()
        self.projects = ProjectRepository()

    @transaction.atomic
    def provision(self, project_id: uuid.UUID, name: str, token: str, scopes: set[str],
                  expires_at: date) -> 'ProjectAPICredential':
        if not name.strip() or not scopes or not scopes <= self.SCOPES:
            raise ValueError('A name and valid scopes are required')
        if not token or len(token) < 43:
            raise ValueError('A high-entropy project credential is required')
        if expires_at < timezone.localdate():
            raise ValueError('Credential expiry must not be in the past')
        project = self.projects.get_project_by_id(project_id)
        if not project or not project.organization.is_active:
            raise ValueError('Active project and organization required')
        digest = hashlib.sha256(token.encode('utf-8')).hexdigest()
        return self.credentials.create(project, name.strip(), digest, expires_at, scopes)

    def authenticate(self, token: str) -> Optional['ProjectAPICredential']:
        if not token or len(token) < 43:
            return None
        digest = hashlib.sha256(token.encode('utf-8')).hexdigest()
        credential = self.credentials.get_by_digest(digest)
        if (credential is None or not secrets.compare_digest(credential.token_digest, digest)
                or credential.revoked_at is not None or credential.expires_at < timezone.localdate()
                or not credential.project.organization.is_active):
            return None
        return credential

    @transaction.atomic
    def revoke(self, project_id: uuid.UUID, credential_id: uuid.UUID) -> bool:
        return self.credentials.revoke(project_id, credential_id, timezone.now())

    def is_deletable(self, credential: 'ProjectAPICredential') -> bool:
        """Business rule: only revoked or expired credentials may be deleted."""
        return bool(credential.revoked_at) or credential.expires_at < timezone.localdate()
