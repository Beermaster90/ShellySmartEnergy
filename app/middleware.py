"""Middleware that turns a verified Cloudflare Access token into a Django login.

If a request arrives already authenticated by Cloudflare Access and carries a
valid signed token, we match the token's email to an existing Django user and
log them in — so behind Access there is no second password prompt.

Only existing users are matched, by email; an Access-authenticated email with no
matching account is NOT auto-provisioned, because accounts here own physical
devices and privilege levels that must be assigned deliberately.
"""

import logging

from django.conf import settings
from django.contrib.auth import get_user_model, login
from django.core.exceptions import MiddlewareNotUsed

from app.cf_access import is_configured, verify_access_jwt

logger = logging.getLogger(__name__)

ACCESS_JWT_HEADER = "HTTP_CF_ACCESS_JWT_ASSERTION"
MODEL_BACKEND = "django.contrib.auth.backends.ModelBackend"


class CloudflareAccessMiddleware:
    """Auto-login the user behind a verified Cloudflare Access token."""

    def __init__(self, get_response):
        self.get_response = get_response
        if not is_configured():
            # Not behind Access (e.g. local dev) — remove from the chain entirely.
            raise MiddlewareNotUsed
        logger.info("Cloudflare Access SSO enabled for aud=%s", settings.CF_ACCESS_AUD)

    def __call__(self, request):
        if not request.user.is_authenticated:
            self._try_access_login(request)
        return self.get_response(request)

    def _try_access_login(self, request):
        token = request.META.get(ACCESS_JWT_HEADER)
        if not token:
            return
        claims = verify_access_jwt(token)
        if not claims:
            return
        email = (claims.get("email") or "").strip()
        if not email:
            return

        user = get_user_model().objects.filter(email__iexact=email, is_active=True).first()
        if user is None:
            # Access let them in, but no local account owns this email. Log it so
            # the operator can create/assign one; do not silently create it.
            logger.warning(
                "Cloudflare Access user %s has no matching Django account", email
            )
            return

        login(request, user, backend=MODEL_BACKEND)
        logger.info("Cloudflare Access SSO login for %s -> %s", email, user.username)
