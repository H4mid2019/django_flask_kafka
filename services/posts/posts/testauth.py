"""Helpers for authenticating in tests.

Tests mint real RS256 tokens with a throwaway key and point the JWKS client at
it. Only the key fetch is stubbed; jwt.decode still checks signature, issuer,
audience and expiry, so the tests exercise the same path a live request takes.
"""

import datetime as dt
import uuid

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from django.conf import settings


class FakeSigningKey:
    def __init__(self, key):
        self.key = key


class FakeJWKClient:
    """Stands in for PyJWKClient so no HTTP call is made."""

    def __init__(self, public_key):
        self._public_key = public_key

    def get_signing_key_from_jwt(self, _token):
        return FakeSigningKey(self._public_key)


class TokenFactory:
    def __init__(self):
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.public_key = self.private_key.public_key()

    def token(self, user_id=None, email="tester@example.com", token_id=None, **overrides):
        now = dt.datetime.now(dt.UTC)
        claims = {
            "sub": str(user_id or uuid.uuid4()),
            "email": email,
            "jti": token_id or str(uuid.uuid4()),
            "iss": settings.AUTH_ISSUER,
            "aud": settings.AUTH_AUDIENCE,
            "iat": now,
            "exp": now + dt.timedelta(hours=1),
        }
        claims.update(overrides)
        return jwt.encode(claims, self.private_key, algorithm="RS256")

    def auth_header(self, **kwargs):
        return {"HTTP_AUTHORIZATION": f"Bearer {self.token(**kwargs)}"}
