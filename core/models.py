import os
from datetime import time as dtime

from django.conf import settings
from django.db import models
from django.utils import timezone

from . import crypto


class Setting(models.Model):
    key = models.CharField(max_length=100, unique=True)
    value = models.JSONField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.key


class SchedulerState(models.Model):
    """Singleton row. Locked (SELECT ... FOR UPDATE) by every scheduler tick so
    that several beat / worker processes can never exceed max_downloads."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1)
    last_tick = models.DateTimeField(null=True, blank=True)

    @classmethod
    def lock(cls):
        cls.objects.get_or_create(id=1)
        return cls.objects.select_for_update().get(id=1)


class Download(models.Model):
    QUEUED, RUNNING, PAUSED, COMPLETED, ERROR, CANCELED = (
        "queued", "running", "paused", "completed", "error", "canceled",
    )
    STATUSES = [(s, s) for s in (QUEUED, RUNNING, PAUSED, COMPLETED, ERROR, CANCELED)]
    ACTIVE = (QUEUED, RUNNING, PAUSED)
    FINISHED = (COMPLETED, ERROR, CANCELED)

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.CASCADE, related_name="downloads")
    url = models.TextField()
    directory = models.CharField(max_length=1024)
    filename = models.CharField(max_length=255)
    options = models.JSONField(default=dict, blank=True)
    http_user = models.CharField(max_length=255, blank=True)
    http_password_enc = models.TextField(blank=True)

    status = models.CharField(max_length=12, choices=STATUSES, default=QUEUED, db_index=True)
    position = models.BigIntegerField(default=0, db_index=True)
    not_before = models.DateTimeField(null=True, blank=True)

    total_bytes = models.BigIntegerField(null=True, blank=True)
    downloaded_bytes = models.BigIntegerField(default=0)
    error = models.TextField(blank=True)
    exit_code = models.IntegerField(null=True, blank=True)
    pid = models.IntegerField(null=True, blank=True)
    requeues = models.PositiveSmallIntegerField(default=0)

    created_at = models.DateTimeField(default=timezone.now)
    dispatched_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["position", "id"]
        indexes = [models.Index(fields=["status", "position"])]

    def __str__(self):
        return f"#{self.pk} {self.filename} [{self.status}]"

    @property
    def path(self):
        return os.path.join(self.directory, self.filename)

    @property
    def log_path(self):
        return os.path.join(settings.WGP_LOG_DIR, f"{self.pk}.log")

    @property
    def http_password(self):
        return crypto.decrypt(self.http_password_enc)

    @http_password.setter
    def http_password(self, value):
        self.http_password_enc = crypto.encrypt(value or "")


class SpeedRule(models.Model):
    """Time window with a per-download speed limit. Active rules lower the effective
    --limit-rate of running downloads (wget is restarted with -c when it changes)."""

    name = models.CharField(max_length=100)
    enabled = models.BooleanField(default=True)
    days = models.CharField(max_length=20, blank=True, help_text="Comma separated 0=Mon..6=Sun, empty = every day")
    start = models.TimeField(default=dtime(0, 0))
    end = models.TimeField(default=dtime(23, 59))
    limit_kbps = models.PositiveIntegerField(default=0, help_text="KB/s per download, 0 = unlimited")

    class Meta:
        ordering = ["start", "id"]

    def day_set(self):
        return {int(x) for x in self.days.split(",") if x.strip().isdigit()}

    def is_active(self, now=None):
        now = timezone.localtime(now or timezone.now())
        days = self.day_set()
        t = now.time().replace(second=0, microsecond=0)
        if self.start <= self.end:
            in_window = self.start <= t <= self.end
            day = now.weekday()
        else:  # spans midnight; the day belongs to the day the window started
            in_window = t >= self.start or t <= self.end
            day = now.weekday() if t >= self.start else (now.weekday() - 1) % 7
        return self.enabled and in_window and (not days or day in days)


class ApiToken(models.Model):
    """Bearer token for the mobile API. Only the SHA-256 of the token is stored."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="api_tokens")
    name = models.CharField(max_length=100, blank=True)  # device name, e.g. "Pixel 8"
    key_hash = models.CharField(max_length=64, unique=True)
    prefix = models.CharField(max_length=12)  # first chars, to recognise a token in lists
    created_at = models.DateTimeField(default=timezone.now)
    last_used_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user_id}:{self.prefix}…"
