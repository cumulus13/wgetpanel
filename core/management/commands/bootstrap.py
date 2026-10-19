"""Idempotent first-run setup: creates download dirs and the admin user from DJANGO_SUPERUSER_*."""
import os

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Create download/log directories and the initial admin user (from DJANGO_SUPERUSER_USERNAME / _PASSWORD / _EMAIL)."

    def handle(self, *args, **opts):
        for d in (settings.WGP_DOWNLOAD_ROOT, settings.WGP_LOG_DIR):
            os.makedirs(d, exist_ok=True)
        User = get_user_model()
        name = os.environ.get("DJANGO_SUPERUSER_USERNAME")
        pw = os.environ.get("DJANGO_SUPERUSER_PASSWORD")
        if name and pw:
            user, created = User.objects.get_or_create(username=name, defaults={"email": os.environ.get("DJANGO_SUPERUSER_EMAIL", ""), "is_staff": True, "is_superuser": True})
            if created:
                user.set_password(pw)
                user.save()
                self.stdout.write(self.style.SUCCESS(f"Created admin user {name}"))
            else:
                self.stdout.write(f"Admin user {name} already exists")
        else:
            self.stdout.write("DJANGO_SUPERUSER_USERNAME/PASSWORD not set, no admin created (use manage.py createsuperuser)")
        if not shutil_which(settings.WGP_WGET_BINARY):
            self.stderr.write(self.style.WARNING(f"wget binary '{settings.WGP_WGET_BINARY}' not found on PATH"))


def shutil_which(binary):
    import shutil
    return shutil.which(binary)
