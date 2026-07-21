"""Outbox relay: moves rows from the posts database into Kafka.

Separated from the posts service on purpose. Publishing is a different failure
domain from serving HTTP: it needs to retry for as long as the broker is down,
and a request handler cannot wait that long. Running it as its own process also
means the API stays up and keeps accepting writes while Kafka is unavailable.
The events simply queue in the outbox and drain when the broker comes back.

Delivery is at-least-once. The relay can publish successfully and die before
recording that it did, and will publish again on restart. Making it
exactly-once would need the broker and the database in one transaction, which is
the thing the outbox exists to avoid. Consumers deduplicate instead.
"""

import json
import logging
import os
import signal
import sys
import time

import psycopg
from kafka import KafkaProducer
from kafka.errors import KafkaError

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(levelname)s %(asctime)s %(name)s %(message)s",
)
logger = logging.getLogger("relay")

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://posts:posts@localhost:5432/posts")
KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
POLL_INTERVAL = float(os.getenv("RELAY_POLL_INTERVAL", "1.0"))
BATCH_SIZE = int(os.getenv("RELAY_BATCH_SIZE", "100"))

_running = True


def _stop(signum, _frame):
    """Finish the batch in flight, then exit."""
    global _running  # noqa: PLW0603
    logger.info("signal %s received, shutting down after this batch", signum)
    _running = False


def build_producer():
    return KafkaProducer(
        bootstrap_servers=KAFKA_BOOTSTRAP.split(","),
        value_serializer=lambda v: json.dumps(v).encode("utf-8"),
        key_serializer=lambda k: k.encode("utf-8") if k else None,
        # Wait for all in-sync replicas. With acks=1 the leader can acknowledge
        # and then fail before replicating, and the event is gone even though
        # the relay recorded it as published.
        acks="all",
        retries=5,
        # Without this, retries can reorder messages, and per-key ordering is
        # the reason events are keyed on the slug at all.
        max_in_flight_requests_per_connection=1,
        linger_ms=10,
    )


def claim_batch(conn, limit):
    """Take a batch of unpublished events, locking them against other relays.

    FOR UPDATE SKIP LOCKED is what allows more than one relay to run: each takes
    rows the others have not, instead of blocking or duplicating work.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, topic, key, payload
              FROM posts_outboxevent
             WHERE published_at IS NULL
             ORDER BY created_at
             LIMIT %s
               FOR UPDATE SKIP LOCKED
            """,
            (limit,),
        )
        return cur.fetchall()


def mark_published(conn, event_ids):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE posts_outboxevent SET published_at = now() WHERE id = ANY(%s)",
            (event_ids,),
        )


def publish_batch(producer, rows):
    """Publish a batch and block until the broker has acknowledged all of it.

    The previous implementation called send() and returned. send() is
    asynchronous, so the process could exit or the producer be collected before
    anything reached the broker, and the event was lost with no error anywhere.
    Every future is resolved here before the rows are marked published.
    """
    futures = []
    for event_id, topic, key, payload in rows:
        # The outbox row id travels with the message so a consumer can spot a
        # republish of the same event even though it lands at a new offset.
        headers = [("event_id", str(event_id).encode())]
        futures.append((event_id, producer.send(topic, key=key, value=payload, headers=headers)))

    producer.flush()

    delivered = []
    for event_id, future in futures:
        try:
            future.get(timeout=30)
            delivered.append(event_id)
        except KafkaError:
            # Left unpublished on purpose, so the next pass retries it.
            logger.exception("publish failed for event %s, will retry", event_id)
    return delivered


def run_once(conn, producer):
    """One pass. Returns how many events were published."""
    with conn.transaction():
        rows = claim_batch(conn, BATCH_SIZE)
        if not rows:
            return 0

        delivered = publish_batch(producer, rows)
        if delivered:
            mark_published(conn, delivered)
        return len(delivered)


def main():
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    producer = build_producer()
    logger.info("relay started, polling every %ss", POLL_INTERVAL)

    with psycopg.connect(DATABASE_URL, autocommit=True) as conn:
        while _running:
            try:
                published = run_once(conn, producer)
                if published:
                    logger.info("published %d event(s)", published)
                else:
                    time.sleep(POLL_INTERVAL)
            except Exception:  # noqa: BLE001
                # A relay that exits on a transient database or broker error
                # stops the whole pipeline. Log it and try again.
                logger.exception("relay pass failed, retrying")
                time.sleep(POLL_INTERVAL)

    producer.flush()
    producer.close()
    logger.info("relay stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
