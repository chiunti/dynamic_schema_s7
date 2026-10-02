from django.core.exceptions import ImproperlyConfigured

from .base import *

DEBUG = False

if not SECRET_KEY:
    raise ImproperlyConfigured(
        'DJANGO_SECRET_KEY environment variable is required in production'
    )
