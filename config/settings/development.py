from .base import *

DEBUG = True

# Dev-only fallback so local runs don't require DJANGO_SECRET_KEY.
SECRET_KEY = SECRET_KEY or 'dev-only-insecure-secret-key'
