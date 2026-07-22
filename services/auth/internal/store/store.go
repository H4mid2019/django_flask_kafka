// Package store persists users, refresh tokens and outbox events.
package store

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"sort"
	"time"

	"github.com/google/uuid"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/H4mid2019/django_flask_kafka/services/auth/migrations"
)

const uniqueViolation = "23505"

var (
	ErrNotFound      = errors.New("not found")
	ErrEmailTaken    = errors.New("email is already registered")
	ErrTokenInvalid  = errors.New("refresh token is not valid")
	TopicTokenRevoke = "token_revoked"
)

type Store struct{ pool *pgxpool.Pool }

type User struct {
	ID           uuid.UUID
	Email        string
	PasswordHash string
}

func New(ctx context.Context, dsn string) (*Store, error) {
	cfg, err := pgxpool.ParseConfig(dsn)
	if err != nil {
		return nil, fmt.Errorf("parse dsn: %w", err)
	}
	cfg.MaxConns = 10
	cfg.MaxConnLifetime = time.Hour

	pool, err := pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		return nil, fmt.Errorf("connect: %w", err)
	}
	return &Store{pool: pool}, nil
}

func (s *Store) Close()                         { s.pool.Close() }
func (s *Store) Ping(ctx context.Context) error { return s.pool.Ping(ctx) }

func (s *Store) CreateUser(ctx context.Context, email, passwordHash string) (*User, error) {
	user := &User{Email: email, PasswordHash: passwordHash}
	err := s.pool.QueryRow(ctx,
		`INSERT INTO users (email, password_hash) VALUES ($1, $2) RETURNING id`,
		email, passwordHash,
	).Scan(&user.ID)

	var pgErr *pgconn.PgError
	if errors.As(err, &pgErr) && pgErr.Code == uniqueViolation {
		return nil, ErrEmailTaken
	}
	if err != nil {
		return nil, fmt.Errorf("insert user: %w", err)
	}
	return user, nil
}

func (s *Store) UserByEmail(ctx context.Context, email string) (*User, error) {
	var user User
	err := s.pool.QueryRow(ctx,
		`SELECT id, email, password_hash FROM users WHERE lower(email) = lower($1)`, email,
	).Scan(&user.ID, &user.Email, &user.PasswordHash)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, fmt.Errorf("select user: %w", err)
	}
	return &user, nil
}

// StoreRefreshToken records the hash of a freshly issued refresh token.
func (s *Store) StoreRefreshToken(ctx context.Context, userID uuid.UUID, hash string, expiresAt time.Time) error {
	_, err := s.pool.Exec(ctx,
		`INSERT INTO refresh_tokens (user_id, token_hash, expires_at) VALUES ($1, $2, $3)`,
		userID, hash, expiresAt)
	if err != nil {
		return fmt.Errorf("insert refresh token: %w", err)
	}
	return nil
}

// ConsumeRefreshToken validates a refresh token and marks it used in one step.
//
// Rotation: the row is revoked as it is redeemed, so a refresh token works
// exactly once. If a stolen token is replayed after the legitimate client has
// already refreshed, it fails here rather than minting a second live session.
func (s *Store) ConsumeRefreshToken(ctx context.Context, hash string) (uuid.UUID, error) {
	var userID uuid.UUID
	err := s.pool.QueryRow(ctx, `
		UPDATE refresh_tokens
		   SET revoked_at = now()
		 WHERE token_hash = $1
		   AND revoked_at IS NULL
		   AND expires_at > now()
		RETURNING user_id`, hash).Scan(&userID)
	if errors.Is(err, pgx.ErrNoRows) {
		return uuid.Nil, ErrTokenInvalid
	}
	if err != nil {
		return uuid.Nil, fmt.Errorf("consume refresh token: %w", err)
	}
	return userID, nil
}

// RevokeAccessToken records a revocation and queues the announcement.
//
// Both in one transaction, for the reason the posts service uses an outbox: a
// revocation stored but never announced leaves the token working everywhere,
// and one announced without being stored cannot be replayed to a service that
// was down at the time.
func (s *Store) RevokeAccessToken(ctx context.Context, tokenID string, userID uuid.UUID, expiresAt time.Time) error {
	payload, err := json.Marshal(map[string]any{
		"token_id":   tokenID,
		"user_id":    userID.String(),
		"expires_at": expiresAt.UTC().Format(time.RFC3339),
	})
	if err != nil {
		return fmt.Errorf("encode revocation: %w", err)
	}

	tx, err := s.pool.Begin(ctx)
	if err != nil {
		return fmt.Errorf("begin: %w", err)
	}
	defer func() { _ = tx.Rollback(ctx) }()

	if _, err := tx.Exec(ctx,
		`UPDATE refresh_tokens SET revoked_at = now() WHERE user_id = $1 AND revoked_at IS NULL`,
		userID); err != nil {
		return fmt.Errorf("revoke refresh tokens: %w", err)
	}

	if _, err := tx.Exec(ctx,
		`INSERT INTO outbox_events (topic, key, payload) VALUES ($1, $2, $3)`,
		TopicTokenRevoke, userID.String(), payload); err != nil {
		return fmt.Errorf("queue revocation event: %w", err)
	}

	if err := tx.Commit(ctx); err != nil {
		return fmt.Errorf("commit: %w", err)
	}
	return nil
}

// OutboxEvent is a pending announcement.
type OutboxEvent struct {
	ID      uuid.UUID
	Topic   string
	Key     string
	Payload []byte
}

// ClaimOutbox takes unpublished events, locking them against other publishers.
func (s *Store) ClaimOutbox(ctx context.Context, limit int) ([]OutboxEvent, error) {
	rows, err := s.pool.Query(ctx, `
		SELECT id, topic, key, payload
		  FROM outbox_events
		 WHERE published_at IS NULL
		 ORDER BY created_at
		 LIMIT $1
		   FOR UPDATE SKIP LOCKED`, limit)
	if err != nil {
		return nil, fmt.Errorf("claim outbox: %w", err)
	}
	defer rows.Close()

	events := []OutboxEvent{}
	for rows.Next() {
		var event OutboxEvent
		if err := rows.Scan(&event.ID, &event.Topic, &event.Key, &event.Payload); err != nil {
			return nil, fmt.Errorf("scan outbox event: %w", err)
		}
		events = append(events, event)
	}
	return events, rows.Err()
}

func (s *Store) MarkPublished(ctx context.Context, ids []uuid.UUID) error {
	if len(ids) == 0 {
		return nil
	}
	_, err := s.pool.Exec(ctx,
		`UPDATE outbox_events SET published_at = now() WHERE id = ANY($1)`, ids)
	if err != nil {
		return fmt.Errorf("mark published: %w", err)
	}
	return nil
}

// Migrate applies the schema files, forward only, one transaction each.
func (s *Store) Migrate(ctx context.Context) error {
	if _, err := s.pool.Exec(ctx,
		`CREATE TABLE IF NOT EXISTS schema_migrations (
			version TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())`); err != nil {
		return fmt.Errorf("create schema_migrations: %w", err)
	}

	names, err := fs.Glob(migrations.FS, "*.sql")
	if err != nil {
		return fmt.Errorf("list migrations: %w", err)
	}
	sort.Strings(names)

	for _, name := range names {
		var exists bool
		if err := s.pool.QueryRow(ctx,
			`SELECT EXISTS (SELECT 1 FROM schema_migrations WHERE version = $1)`, name,
		).Scan(&exists); err != nil {
			return fmt.Errorf("check %s: %w", name, err)
		}
		if exists {
			continue
		}

		body, err := migrations.FS.ReadFile(name)
		if err != nil {
			return fmt.Errorf("read %s: %w", name, err)
		}
		tx, err := s.pool.Begin(ctx)
		if err != nil {
			return fmt.Errorf("begin %s: %w", name, err)
		}
		if _, err := tx.Exec(ctx, string(body)); err != nil {
			_ = tx.Rollback(ctx)
			return fmt.Errorf("apply %s: %w", name, err)
		}
		if _, err := tx.Exec(ctx,
			`INSERT INTO schema_migrations (version) VALUES ($1)`, name); err != nil {
			_ = tx.Rollback(ctx)
			return fmt.Errorf("record %s: %w", name, err)
		}
		if err := tx.Commit(ctx); err != nil {
			return fmt.Errorf("commit %s: %w", name, err)
		}
	}
	return nil
}

func (s *Store) UserByID(ctx context.Context, id uuid.UUID) (*User, error) {
	var user User
	err := s.pool.QueryRow(ctx,
		`SELECT id, email, password_hash FROM users WHERE id = $1`, id,
	).Scan(&user.ID, &user.Email, &user.PasswordHash)
	if errors.Is(err, pgx.ErrNoRows) {
		return nil, ErrNotFound
	}
	if err != nil {
		return nil, fmt.Errorf("select user by id: %w", err)
	}
	return &user, nil
}
