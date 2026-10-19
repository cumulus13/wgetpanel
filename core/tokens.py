"""Bearer tokens for the mobile API."""
import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .models import ApiToken

PREFIX = "wgp_"
TOUCH_EVERY = timedelta(minutes=1)  # do not write last_used_at on every request


def _hash(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def issue(user, name=""):
    raw = PREFIX + secrets.token_urlsafe(32)
    ttl = settings.WGP_TOKEN_TTL_DAYS
    tok = ApiToken.objects.create(
        user=user, name=(name or "")[:100], key_hash=_hash(raw), prefix=raw[:8],
        expires_at=timezone.now() + timedelta(days=ttl) if ttl else None)
    return tok, raw


def authenticate(request):
    """ApiToken for a valid ``Authorization: Bearer`` header, else None. Cookies are never consulted."""
    header = request.META.get("HTTP_AUTHORIZATION", "")
    scheme, _, raw = header.partition(" ")
    raw = raw.strip()
    if scheme.lower() != "bearer" or not raw.startswith(PREFIX) or len(raw) > 200:
        return None
    tok = ApiToken.objects.select_related("user").filter(key_hash=_hash(raw)).first()
    if tok is None or not tok.user.is_active:
        return None
    now = timezone.now()
    if tok.expires_at and tok.expires_at <= now:
        return None
    if tok.last_used_at is None or now - tok.last_used_at > TOUCH_EVERY:
        ApiToken.objects.filter(pk=tok.pk).update(last_used_at=now)
    return tok
