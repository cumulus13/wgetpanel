"""Django settings for wgetpanel.

Only what is needed to reach the database / broker lives in the environment.
Everything else (site name, limits, rules ...) is stored in the database,
see ``core/conf.py``.
"""
import os
import sys
from pathlib import Path
from envdot import load_env

import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent
load_env(str(BASE_DIR / '.env'))

# config = load_env(str(BASE_DIR / '.env'))

def env(name, default=None):
    return os.getenv(name, default)


def env_bool(name, default=False):
    return str(env(name, "1" if default else "0")).lower() in ("1", "true", "yes", "on")


DEBUG = env_bool("DJANGO_DEBUG", False)
print(f"DEBUG: {DEBUG} | type: {type(DEBUG)}")
SECRET_KEY = env("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = "insecure-dev-key-do-not-use-in-production"
    else:
        raise RuntimeError("DJANGO_SECRET_KEY must be set (or DJANGO_DEBUG=1 for development)")

ALLOWED_HOSTS = os.getenv("DJANGO_ALLOWED_HOSTS", ["*"], cast_type=list)
print(f"ALLOWED_HOSTS: {ALLOWED_HOSTS} | type: {type(ALLOWED_HOSTS)}")
CSRF_TRUSTED_ORIGINS = [o.strip() for o in env("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()]

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "core",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "wgetpanel.urls"
WSGI_APPLICATION = "wgetpanel.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.panel",
            ],
        },
    },
]

DATABASES = {
    "default": dj_database_url.parse(
        env("DATABASE_URL", f"sqlite:///{BASE_DIR / 'db.sqlite3'}"),
        conn_max_age=int(env("DB_CONN_MAX_AGE", "60")),
        conn_health_checks=True,
    )
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = env("TZ", env("TIME_ZONE", "UTC"))
print(f"TIME_ZONE: {TIME_ZONE}")
# TIME_ZONE = "Asia/Jakarta"
USE_I18N = False
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
TESTING = len(sys.argv) > 1 and sys.argv[1] == "test"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        # hashed file names + compression in production (needs `collectstatic`), plain files in dev / tests
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage" if (DEBUG or TESTING)
        else "whitenoise.storage.CompressedManifestStaticFilesStorage"
    },
}

# --- security -------------------------------------------------------------
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_HTTPONLY = True  # the token is delivered through a <meta> tag
CSRF_COOKIE_SAMESITE = "Lax"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
if env_bool("DJANGO_BEHIND_PROXY", False):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    USE_X_FORWARDED_HOST = True
SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = env_bool("DJANGO_SECURE_COOKIES", False)
SECURE_SSL_REDIRECT = env_bool("DJANGO_SSL_REDIRECT", False)
SECURE_HSTS_SECONDS = int(env("DJANGO_HSTS_SECONDS", "0"))
SECURE_HSTS_INCLUDE_SUBDOMAINS = SECURE_HSTS_SECONDS > 0

# --- wgetpanel ------------------------------------------------------------
# Field encryption key (http passwords). Falls back to a key derived from SECRET_KEY.
FIELD_ENCRYPTION_KEY = env("FIELD_ENCRYPTION_KEY", "")
# Default root for downloads; further directories are configured in the Settings page.
WGP_DOWNLOAD_ROOT = env("WGP_DOWNLOAD_ROOT", str(BASE_DIR / "downloads"))
# Where per-download wget logs live (must be readable by web + worker).
WGP_LOG_DIR = env("WGP_LOG_DIR", str(BASE_DIR / "logs"))
WGP_WGET_BINARY = env("WGP_WGET_BINARY", "wget")
# Reject URLs resolving to loopback / private / link-local addresses (SSRF guard).
WGP_BLOCK_PRIVATE_ADDRESSES = env_bool("WGP_BLOCK_PRIVATE_ADDRESSES", True)
WGP_SCHEDULER_TICK = os.getenv("WGP_SCHEDULER_TICK", '2.0', cast_type=float)
WGP_REDIS_URL = env("WGP_REDIS_URL", env("REDIS_URL", "redis://localhost:6379/1"))

# --- celery ---------------------------------------------------------------
CELERY_BROKER_URL = env("CELERY_BROKER_URL", env("REDIS_URL", "redis://localhost:6379/0"))
CELERY_RESULT_BACKEND = None
CELERY_TASK_IGNORE_RESULT = True
CELERY_TASK_ACKS_LATE = False  # long running tasks + redis visibility_timeout = duplicates; the reaper handles lost workers
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_DEFAULT_QUEUE = "celery"
CELERY_TASK_ROUTES = {"core.tasks.run_download": {"queue": "downloads"}}
CELERY_TASK_ALWAYS_EAGER = False
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
CELERY_TIMEZONE = env("TZ", env("TIME_ZONE", "UTC"))
CELERY_BEAT_SCHEDULE = {
    "scheduler-tick": {"task": "core.tasks.scheduler_tick", "schedule": WGP_SCHEDULER_TICK},
    "reap-stale": {"task": "core.tasks.reap_stale", "schedule": 10.0},
    "maintenance": {"task": "core.tasks.maintenance", "schedule": 3600.0},
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"std": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "std"}},
    "root": {"handlers": ["console"], "level": env("DJANGO_LOG_LEVEL", "INFO")},
}
