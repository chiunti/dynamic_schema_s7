"""Management command to create or retrieve a DRF auth token for a user."""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from rest_framework.authtoken.models import Token


class Command(BaseCommand):
    help = "Create or retrieve a DRF token for a user by email."

    def add_arguments(self, parser):
        parser.add_argument(
            "email",
            type=str,
            help="Email address of the user (USERNAME_FIELD is email).",
        )

    def handle(self, *args, **options):
        email = options["email"].strip()
        if not email:
            raise CommandError("Email is required.")

        User = get_user_model()
        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            raise CommandError(f"User with email {email} does not exist.")

        token, created = Token.objects.get_or_create(user=user)
        action = "Created" if created else "Retrieved"
        self.stdout.write(
            self.style.SUCCESS(f"{action} token for {email}: {token.key}")
        )
