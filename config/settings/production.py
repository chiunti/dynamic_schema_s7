from django.core.exceptions import ImproperlyConfigured

from .base import *

DEBUG = False

if not SECRET_KEY:
    raise ImproperlyConfigured(
        'DJANGO_SECRET_KEY environment variable is required in production'
    )

# Behind a TLS-terminating reverse proxy (Coolify/Traefik forwards over HTTP):
# trust X-Forwarded-Proto so request.is_secure() reflects the original scheme.
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
USE_X_FORWARDED_HOST = True

# Django 4+ verifies the Origin header on unsafe requests; each origin needs
# its scheme, e.g. DJANGO_CSRF_TRUSTED_ORIGINS=https://s7.example.com
CSRF_TRUSTED_ORIGINS = [
    o.strip()
    for o in os.environ.get('DJANGO_CSRF_TRUSTED_ORIGINS', '').split(',')
    if o.strip()
]

SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
