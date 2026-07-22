"""Local verification of tokens issued by the Go auth service.

Nothing here calls the auth service per request. Its public key is fetched from
the JWKS endpoint once, cached, and every request after that is verified in this
process.

Asking auth to validate each request would add a network hop to every call and
make auth a hard dependency: auth restarts, this service stops serving.
Signature verification needs only the public key, so there is no reason to pay
that.

Revocation is the one thing local verification cannot do alone, because a signed
token stays valid until it expires. The token_revoked consumer writes to the
RevokedToken table below and this module checks it.
"""

import logging

import jwt
from django.conf import settings
from django.utils import timezone
from jwt import PyJWKClient
from rest_framework import authentication, exceptions

from .models import RevokedToken

logger = logging.getLogger(__name__)

_jwk_client: PyJWKClient | None = None


def jwk_client() -> PyJWKClient:
    """One client per process, so the key is fetched once and cached."""
    global _jwk_client  # noqa: PLW0603
    if _jwk_client is None:
        _jwk_client = PyJWKClient(settings.AUTH_JWKS_URL, cache_keys=True, lifespan=300)
    return _jwk_client


class TokenUser:
    """The caller, as described by a verified token.

    Deliberately not a database row. This service has no users table and should
    not have one: copying the auth service's data here would create a second
    copy to keep in step. The token carries what is needed.
    """

    is_authenticated = True

    def __init__(self, user_id: str, email: str, token_id: str):
        self.id = user_id
        self.email = email
        self.token_id = token_id

    def __str__(self) -> str:
        return self.email


class JWTAuthentication(authentication.BaseAuthentication):
    keyword = "Bearer"

    def authenticate(self, request):
        header = request.META.get("HTTP_AUTHORIZATION", "")
        if not header.lower().startswith("bearer "):
            return None

        token = header[7:].strip()
        if not token:
            raise exceptions.AuthenticationFailed("bearer token is empty")

        try:
            signing_key = jwk_client().get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                # Pinned. Trusting the header's alg is how "none" and
                # HMAC-with-the-public-key forgeries get accepted.
                algorithms=["RS256"],
                audience=settings.AUTH_AUDIENCE,
                issuer=settings.AUTH_ISSUER,
                options={"require": ["exp", "sub", "jti"]},
            )
        except jwt.PyJWTError as exc:
            raise exceptions.AuthenticationFailed(f"token is not valid: {exc}") from exc
        except Exception as exc:
            # A JWKS fetch failure is not the caller's fault, so do not report
            # it as a bad token.
            logger.exception("could not verify token")
            raise exceptions.AuthenticationFailed("cannot verify token right now") from exc

        token_id = claims.get("jti", "")
        if RevokedToken.is_revoked(token_id):
            raise exceptions.AuthenticationFailed("token has been revoked")

        return TokenUser(claims["sub"], claims.get("email", ""), token_id), token

    def authenticate_header(self, request):
        """Makes DRF answer 401 rather than 403 when credentials are missing.

        Without this DRF has no WWW-Authenticate header to send and reports 403,
        which says "you may not" when the truth is "you did not say who you are".
        """
        return self.keyword


def purge_expired_revocations() -> int:
    """Drop denylist rows for tokens that have expired on their own.

    Without this the table grows forever. An entry is only useful until the
    token would have stopped verifying anyway.
    """
    deleted, _ = RevokedToken.objects.filter(expires_at__lte=timezone.now()).delete()
    return deleted
