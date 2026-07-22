// Command authd issues tokens and announces revocations.
package main

import (
	"context"
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/pem"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"github.com/H4mid2019/django_flask_kafka/services/auth/internal/api"
	"github.com/H4mid2019/django_flask_kafka/services/auth/internal/auth"
	"github.com/H4mid2019/django_flask_kafka/services/auth/internal/store"
)

func main() {
	log := slog.New(slog.NewJSONHandler(os.Stdout, nil))
	if err := run(log); err != nil {
		log.Error("fatal", "error", err)
		os.Exit(1)
	}
}

func run(log *slog.Logger) error {
	var (
		dsn       = env("DATABASE_URL", "postgres://auth:auth@localhost:5432/auth?sslmode=disable")
		addr      = env("LISTEN_ADDR", ":8080")
		issuerURL = env("AUTH_ISSUER", "http://auth:8080")
		audience  = env("AUTH_AUDIENCE", "django-flask-kafka")
		keyPath   = env("AUTH_PRIVATE_KEY_FILE", "")
		brokers   = env("KAFKA_BOOTSTRAP_SERVERS", "kafka:9092")
	)

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	key, err := loadOrCreateKey(keyPath, log)
	if err != nil {
		return err
	}

	db, err := connectWithRetry(ctx, log, dsn, 30*time.Second)
	if err != nil {
		return err
	}
	defer db.Close()

	if err := db.Migrate(ctx); err != nil {
		return err
	}
	log.Info("migrations applied")

	issuer := auth.NewIssuer(key, issuerURL, audience, accessTTL())
	srv := &http.Server{
		Addr:              addr,
		Handler:           api.New(db, issuer, refreshTTL(), log).Routes(),
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       15 * time.Second,
		WriteTimeout:      15 * time.Second,
	}

	relay := NewRelay(db, brokers, log)
	go relay.Run(ctx)

	httpErr := make(chan error, 1)
	go func() {
		log.Info("listening", "addr", addr, "kid", auth.KeyID(&key.PublicKey))
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			httpErr <- err
		}
	}()

	select {
	case err := <-httpErr:
		return err
	case <-ctx.Done():
		log.Info("shutting down")
	}

	shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	return srv.Shutdown(shutdownCtx)
}

// loadOrCreateKey reads the signing key, or generates one for local runs.
//
// A generated key is fine for development and wrong for anything else: it
// changes on every restart, so every previously issued token stops verifying.
// Production passes AUTH_PRIVATE_KEY_FILE.
func loadOrCreateKey(path string, log *slog.Logger) (*rsa.PrivateKey, error) {
	if path == "" {
		log.Warn("AUTH_PRIVATE_KEY_FILE not set, generating an ephemeral key",
			"consequence", "tokens issued before a restart will stop verifying")
		return rsa.GenerateKey(rand.Reader, 2048)
	}

	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	block, _ := pem.Decode(raw)
	if block == nil {
		return nil, errors.New("private key file is not PEM")
	}

	parsed, err := x509.ParsePKCS8PrivateKey(block.Bytes)
	if err != nil {
		key, err2 := x509.ParsePKCS1PrivateKey(block.Bytes)
		if err2 != nil {
			return nil, errors.New("private key is neither PKCS8 nor PKCS1")
		}
		return key, nil
	}

	key, ok := parsed.(*rsa.PrivateKey)
	if !ok {
		return nil, errors.New("private key is not RSA")
	}
	return key, nil
}

func connectWithRetry(ctx context.Context, log *slog.Logger, dsn string, limit time.Duration) (*store.Store, error) {
	deadline := time.Now().Add(limit)
	for attempt := 1; ; attempt++ {
		db, err := store.New(ctx, dsn)
		if err == nil {
			if err = db.Ping(ctx); err == nil {
				return db, nil
			}
			db.Close()
		}
		if time.Now().After(deadline) {
			return nil, err
		}
		log.Info("database not ready, retrying", "attempt", attempt)
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(time.Second):
		}
	}
}

func accessTTL() time.Duration {
	if seconds, err := strconv.Atoi(os.Getenv("ACCESS_TTL_SECONDS")); err == nil && seconds > 0 {
		return time.Duration(seconds) * time.Second
	}
	return auth.DefaultAccessTTL
}

func refreshTTL() time.Duration {
	if seconds, err := strconv.Atoi(os.Getenv("REFRESH_TTL_SECONDS")); err == nil && seconds > 0 {
		return time.Duration(seconds) * time.Second
	}
	return auth.DefaultRefreshTTL
}

func env(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}
