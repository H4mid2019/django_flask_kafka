package main

import (
	"context"
	"log/slog"
	"strings"
	"time"

	"github.com/google/uuid"
	"github.com/segmentio/kafka-go"

	"github.com/H4mid2019/django_flask_kafka/services/auth/internal/store"
)

// Relay publishes queued revocations to Kafka.
//
// Same shape as the posts service relay, and for the same reason: recording a
// revocation and announcing it are two systems, so they are separated by a
// table rather than attempted together. In-process here rather than its own
// container because the volume is one message per logout, not one per write.
type Relay struct {
	store  *store.Store
	writer *kafka.Writer
	log    *slog.Logger
}

func NewRelay(st *store.Store, brokers string, log *slog.Logger) *Relay {
	return &Relay{
		store: st,
		writer: &kafka.Writer{
			Addr:  kafka.TCP(strings.Split(brokers, ",")...),
			Topic: store.TopicTokenRevoke,
			// Wait for all in-sync replicas. A revocation acknowledged by the
			// leader alone can vanish if that leader fails, and a revocation
			// that vanishes is a token that stays valid.
			RequiredAcks: kafka.RequireAll,
			Balancer:     &kafka.Hash{},
			BatchTimeout: 200 * time.Millisecond,
		},
		log: log,
	}
}

func (r *Relay) Run(ctx context.Context) {
	ticker := time.NewTicker(time.Second)
	defer ticker.Stop()
	defer func() { _ = r.writer.Close() }()

	for {
		select {
		case <-ctx.Done():
			r.log.Info("revocation relay stopped")
			return
		case <-ticker.C:
			if err := r.publishBatch(ctx); err != nil {
				// Transient broker or database trouble should not kill the
				// relay; the rows stay unpublished and the next tick retries.
				r.log.Error("publishing revocations failed, will retry", "error", err)
			}
		}
	}
}

func (r *Relay) publishBatch(ctx context.Context) error {
	events, err := r.store.ClaimOutbox(ctx, 100)
	if err != nil || len(events) == 0 {
		return err
	}

	messages := make([]kafka.Message, 0, len(events))
	ids := make([]uuid.UUID, 0, len(events))
	for _, event := range events {
		messages = append(messages, kafka.Message{
			Key:   []byte(event.Key),
			Value: event.Payload,
			// The outbox row id, so a consumer can recognise a republish of the
			// same revocation.
			Headers: []kafka.Header{{Key: "event_id", Value: []byte(event.ID.String())}},
		})
		ids = append(ids, event.ID)
	}

	// WriteMessages blocks until the broker has acknowledged, so rows are only
	// marked published once the data is actually there.
	if err := r.writer.WriteMessages(ctx, messages...); err != nil {
		return err
	}

	if err := r.store.MarkPublished(ctx, ids); err != nil {
		return err
	}
	r.log.Info("published revocations", "count", len(ids))
	return nil
}
