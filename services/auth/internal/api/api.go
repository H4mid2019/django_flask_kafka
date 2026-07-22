// Package api exposes the auth service over HTTP.
package api

import (
	"encoding/json"
	"errors"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/google/uuid"

	"github.com/H4mid2019/django_flask_kafka/services/auth/internal/auth"
	"github.com/H4mid2019/django_flask_kafka/services/auth/internal/store"
)

type Server struct {
	store      *store.Store
	issuer     *auth.Issuer
	refreshTTL time.Duration
	log        *slog.Logger
}

func New(st *store.Store, issuer *auth.Issuer, refreshTTL time.Duration, log *slog.Logger) *Server {
	if refreshTTL <= 0 {
		refreshTTL = auth.DefaultRefreshTTL
	}
	return &Server{store: st, issuer: issuer, refreshTTL: refreshTTL, log: log}
}

func (s *Server) Routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", s.health)
	// The endpoint that makes local verification possible everywhere else.
	mux.HandleFunc("GET /.well-known/jwks.json", s.jwks)
	mux.HandleFunc("POST /register", s.register)
	mux.HandleFunc("POST /login", s.login)
	mux.HandleFunc("POST /refresh", s.refresh)
	mux.HandleFunc("POST /revoke", s.revoke)
	return mux
}

func (s *Server) health(w http.ResponseWriter, r *http.Request) {
	if err := s.store.Ping(r.Context()); err != nil {
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"status": "database unreachable"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) jwks(w http.ResponseWriter, r *http.Request) {
	// Cacheable on purpose. Verifiers should hold this, not fetch it per
	// request, which is the whole point of signing rather than introspecting.
	w.Header().Set("Cache-Control", "public, max-age=300")
	writeJSON(w, http.StatusOK, s.issuer.PublicJWKS())
}

type credentials struct {
	Email    string `json:"email"`
	Password string `json:"password"`
}

func (s *Server) register(w http.ResponseWriter, r *http.Request) {
	var body credentials
	if !decode(w, r, &body) {
		return
	}
	body.Email = strings.TrimSpace(body.Email)

	if body.Email == "" || !strings.Contains(body.Email, "@") {
		writeError(w, http.StatusBadRequest, "a valid email is required")
		return
	}
	if len(body.Password) < 12 {
		writeError(w, http.StatusBadRequest, "password must be at least 12 characters")
		return
	}

	hash, err := auth.HashPassword(body.Password)
	if err != nil {
		s.fail(w, err)
		return
	}

	user, err := s.store.CreateUser(r.Context(), body.Email, hash)
	if errors.Is(err, store.ErrEmailTaken) {
		writeError(w, http.StatusConflict, "email is already registered")
		return
	}
	if err != nil {
		s.fail(w, err)
		return
	}

	writeJSON(w, http.StatusCreated, map[string]string{"id": user.ID.String(), "email": user.Email})
}

func (s *Server) login(w http.ResponseWriter, r *http.Request) {
	var body credentials
	if !decode(w, r, &body) {
		return
	}

	user, err := s.store.UserByEmail(r.Context(), body.Email)
	if err != nil {
		if errors.Is(err, store.ErrNotFound) {
			// Same response and roughly the same work as a wrong password, so
			// the endpoint does not reveal which addresses are registered.
			_ = auth.VerifyPassword(body.Password, dummyHash)
			writeError(w, http.StatusUnauthorized, "invalid credentials")
			return
		}
		s.fail(w, err)
		return
	}

	if err := auth.VerifyPassword(body.Password, user.PasswordHash); err != nil {
		writeError(w, http.StatusUnauthorized, "invalid credentials")
		return
	}

	s.issueTokens(w, r, user.ID, user.Email)
}

func (s *Server) refresh(w http.ResponseWriter, r *http.Request) {
	var body struct {
		RefreshToken string `json:"refresh_token"`
	}
	if !decode(w, r, &body) {
		return
	}

	userID, err := s.store.ConsumeRefreshToken(r.Context(), auth.HashRefreshToken(body.RefreshToken))
	if errors.Is(err, store.ErrTokenInvalid) {
		writeError(w, http.StatusUnauthorized, "refresh token is not valid")
		return
	}
	if err != nil {
		s.fail(w, err)
		return
	}

	user, err := s.store.UserByID(r.Context(), userID)
	if err != nil {
		s.fail(w, err)
		return
	}
	s.issueTokens(w, r, user.ID, user.Email)
}

func (s *Server) revoke(w http.ResponseWriter, r *http.Request) {
	token := bearerToken(r)
	if token == "" {
		writeError(w, http.StatusUnauthorized, "bearer token required")
		return
	}

	claims, err := s.issuer.Verify(token)
	if err != nil {
		writeError(w, http.StatusUnauthorized, "token is not valid")
		return
	}

	userID, err := uuid.Parse(claims.Subject)
	if err != nil {
		writeError(w, http.StatusUnauthorized, "token subject is not a uuid")
		return
	}

	expires := time.Now().Add(auth.DefaultAccessTTL)
	if claims.ExpiresAt != nil {
		expires = claims.ExpiresAt.Time
	}

	if err := s.store.RevokeAccessToken(r.Context(), claims.ID, userID, expires); err != nil {
		s.fail(w, err)
		return
	}

	s.log.Info("token revoked", "user", userID, "token", claims.ID)
	// Accepted, not OK: the token is refused here immediately, but the other
	// services learn about it when the event reaches them.
	writeJSON(w, http.StatusAccepted, map[string]string{"status": "revocation queued"})
}

func (s *Server) issueTokens(w http.ResponseWriter, r *http.Request, userID uuid.UUID, email string) {
	access, _, err := s.issuer.IssueAccess(userID, email, time.Now())
	if err != nil {
		s.fail(w, err)
		return
	}

	refresh, hash, err := auth.NewRefreshToken()
	if err != nil {
		s.fail(w, err)
		return
	}

	expires := time.Now().Add(s.refreshTTL)
	if err := s.store.StoreRefreshToken(r.Context(), userID, hash, expires); err != nil {
		s.fail(w, err)
		return
	}

	writeJSON(w, http.StatusOK, map[string]any{
		"access_token":  access,
		"refresh_token": refresh,
		"token_type":    "Bearer",
		"expires_in":    int(auth.DefaultAccessTTL.Seconds()),
	})
}

func bearerToken(r *http.Request) string {
	header := r.Header.Get("Authorization")
	if len(header) < 7 || !strings.EqualFold(header[:7], "bearer ") {
		return ""
	}
	return strings.TrimSpace(header[7:])
}

func (s *Server) fail(w http.ResponseWriter, err error) {
	s.log.Error("request failed", "error", err)
	writeError(w, http.StatusInternalServerError, "internal error")
}

func decode(w http.ResponseWriter, r *http.Request, dst any) bool {
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, 1<<20))
	dec.DisallowUnknownFields()
	if err := dec.Decode(dst); err != nil {
		writeError(w, http.StatusBadRequest, "invalid json")
		return false
	}
	return true
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, map[string]string{"error": msg})
}

// dummyHash is verified when the email is unknown, so a login attempt for a
// non-existent account costs roughly the same as one for a real account.
// Without it the response time distinguishes registered addresses from
// unregistered ones, which is a free user enumeration oracle.
var dummyHash = mustDummyHash()

func mustDummyHash() string {
	hash, err := auth.HashPassword("a password nobody has, used only for timing")
	if err != nil {
		panic("cannot build dummy hash: " + err.Error())
	}
	return hash
}
