CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE users (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email         TEXT        NOT NULL,
    password_hash TEXT        NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Unique on the lowercased address rather than the raw string, so Alice@x.com
-- and alice@x.com cannot both register. A plain UNIQUE on email would let them.
CREATE UNIQUE INDEX users_email_lower_uniq ON users (lower(email));

-- Refresh tokens are stored hashed, never in the clear. A database dump should
-- not hand over working credentials, and a refresh token is a credential.
CREATE TABLE refresh_tokens (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    token_hash TEXT        NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX refresh_tokens_user_idx ON refresh_tokens (user_id) WHERE revoked_at IS NULL;

-- An access token is a signed JWT and cannot be withdrawn once issued, so
-- revoking one means telling every service to refuse it until it would have
-- expired anyway.
--
-- Those notifications go through the same outbox the posts service uses, for
-- the same reason: the database write and the broker publish are not one
-- transaction, and a revocation that was recorded but never announced is a
-- token that stays valid everywhere.
CREATE TABLE outbox_events (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    topic        TEXT        NOT NULL,
    key          TEXT        NOT NULL,
    payload      JSONB       NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at TIMESTAMPTZ
);

CREATE INDEX outbox_unpublished_idx ON outbox_events (created_at) WHERE published_at IS NULL;
