"""Local verification of tokens issued by the Go auth service.

Same design as the posts service: fetch the public key from JWKS once, cache it,
verify every request in this process. The auth service is never called per
request, so it can restart without taking this service down.
"""

import functools
import logging
import os

import jwt
from app import RevokedToken, db
from flask import g, jsonify, request
from jwt import PyJWKClient

logger = logging.getLogger(__name__)

JWKS_URL = os.getenv("AUTH_JWKS_URL", "http://auth:8080/.well-known/jwks.json")
ISSUER = os.getenv("AUTH_ISSUER", "http://auth:8080")
AUDIENCE = os.getenv("AUTH_AUDIENCE", "django-flask-kafka")

_client = None


def jwk_client():
    global _client  # noqa: PLW0603
    if _client is None:
        _client = PyJWKClient(JWKS_URL, cache_keys=True, lifespan=300)
    return _client


def verify(token):
    """Return the claims, or raise jwt.PyJWTError."""
    signing_key = jwk_client().get_signing_key_from_jwt(token)
    return jwt.decode(
        token,
        signing_key.key,
        # Pinned, so a token claiming alg=none or HMAC cannot be accepted.
        algorithms=["RS256"],
        audience=AUDIENCE,
        issuer=ISSUER,
        options={"require": ["exp", "sub", "jti"]},
    )


def requires_auth(view):
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        if not header.lower().startswith("bearer "):
            return jsonify({"error": "bearer token required"}), 401

        try:
            claims = verify(header[7:].strip())
        except jwt.PyJWTError as exc:
            return jsonify({"error": f"token is not valid: {exc}"}), 401
        except Exception:
            logger.exception("could not verify token")
            return jsonify({"error": "cannot verify token right now"}), 503

        if db.session.get(RevokedToken, claims.get("jti", "")) is not None:
            return jsonify({"error": "token has been revoked"}), 401

        g.user_id = claims["sub"]
        g.email = claims.get("email", "")
        return view(*args, **kwargs)

    return wrapper
