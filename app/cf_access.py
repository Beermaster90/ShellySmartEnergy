"""Verify Cloudflare Access identity tokens.

When the app sits behind Cloudflare Access, every request Access forwards carries
a signed JWT in the ``Cf-Access-Jwt-Assertion`` header. Verifying that token's
signature against Cloudflare's public keys proves the request really came through
Access for an authenticated user — so we can log that user straight in and skip
the app's own password form.

We deliberately verify the JWT rather than trusting the plaintext
``Cf-Access-Authenticated-User-Email`` header: the header can be forged by
anything that reaches the origin directly, the signed token cannot.
"""

import logging
import threading

import jwt
from django.conf import settings

logger = logging.getLogger(__name__)

_jwk_client = None
_jwk_lock = threading.Lock()


def is_configured() -> bool:
    return bool(
        getattr(settings, "CF_ACCESS_TEAM_DOMAIN", "")
        and getattr(settings, "CF_ACCESS_AUD", "")
    )


def _certs_url() -> str:
    return f"https://{settings.CF_ACCESS_TEAM_DOMAIN}/cdn-cgi/access/certs"


def _issuer() -> str:
    return f"https://{settings.CF_ACCESS_TEAM_DOMAIN}"


def _get_signing_key(token: str):
    """Return the RSA public key for this token, via PyJWT's caching JWKS client.

    Split out so tests can substitute a local key without hitting the network.
    """
    global _jwk_client
    if _jwk_client is None:
        with _jwk_lock:
            if _jwk_client is None:
                # PyJWKClient caches keys and refreshes on unknown kid.
                _jwk_client = jwt.PyJWKClient(_certs_url())
    return _jwk_client.get_signing_key_from_jwt(token).key


def verify_access_jwt(token: str) -> dict | None:
    """Return the token's claims if it is a valid Access token, else None.

    Checks the signature, that the audience matches THIS application's AUD tag
    (so a token minted for another Access app cannot be replayed here), the
    issuer, and expiry.
    """
    if not token or not is_configured():
        return None
    try:
        signing_key = _get_signing_key(token)
        return jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            audience=settings.CF_ACCESS_AUD,
            issuer=_issuer(),
            options={"require": ["exp", "iat", "aud", "iss"]},
            leeway=30,
        )
    except jwt.InvalidTokenError as exc:
        # Wrong audience, bad signature, expired, etc. — treat as unauthenticated.
        logger.warning("Cloudflare Access token rejected: %s", exc)
        return None
    except jwt.PyJWKClientConnectionError as exc:
        # The usual cause is a stale CF_ACCESS_TEAM_DOMAIN: renaming/recreating
        # the Zero Trust team changes it, and the old certs URL then 404s. SSO
        # quietly falls back to the password form, so say so plainly.
        logger.error(
            "Cloudflare Access verification error: cannot fetch signing keys from "
            "%s (%s). Check CF_ACCESS_TEAM_DOMAIN in deploy.env matches the team "
            "domain Access redirects to.",
            _certs_url(),
            exc,
        )
        return None
    except Exception as exc:  # other network / JWKS failures
        logger.error("Cloudflare Access verification error: %s", exc)
        return None
