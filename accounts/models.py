from django.contrib.auth.base_user import BaseUserManager
from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models
from django.utils import timezone

from .constants import (
    ERR_EMAIL_REQUIRED,
    ERR_SUPERUSER_IS_STAFF_REQUIRED,
    ERR_SUPERUSER_IS_SUPERUSER_REQUIRED,
)

# Create your models here.


class CustomUserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError(ERR_EMAIL_REQUIRED)
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', False)
        extra_fields.setdefault('is_superuser', False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)

        if extra_fields.get('is_staff') is not True:
            raise ValueError(ERR_SUPERUSER_IS_STAFF_REQUIRED)
        if extra_fields.get('is_superuser') is not True:
            raise ValueError(ERR_SUPERUSER_IS_SUPERUSER_REQUIRED)

        return self._create_user(email, password, **extra_fields)


class CustomUser(AbstractBaseUser, PermissionsMixin):
    email = models.EmailField(unique=True)

    first_name = models.CharField(max_length=30, blank=True, null=True)
    last_name = models.CharField(max_length=30, blank=True, null=True)

    is_staff = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    date_joined = models.DateTimeField(default=timezone.now)

    objects = CustomUserManager()

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = []

    def get_organizations(self):
        """Get organizations for this user using repository layer."""
        from schemas.repositories.multi_tenant_repository import MultiTenantRepository
        return MultiTenantRepository().get_accessible_organizations(self)

    def is_member_of(self, organization_id) -> bool:
        """Check if user is member of organization using repository layer."""
        from schemas.repositories.multi_tenant_repository import MultiTenantRepository
        return MultiTenantRepository().is_member_of(self, organization_id)

    def get_role_in(self, organization_id):
        """Get user's role in organization using repository layer."""
        from schemas.repositories.multi_tenant_repository import MultiTenantRepository
        return MultiTenantRepository().get_role_in(self, organization_id)

    def can_access_organization(self, organization_id) -> bool:
        """Check if user can access organization using repository layer."""
        return self.is_member_of(organization_id)

    def __str__(self):
        return self.email
