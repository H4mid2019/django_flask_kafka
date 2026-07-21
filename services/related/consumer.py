"""Consumes post events and maintains the related-posts table.

Three things make this safe to restart and safe to scale:

* a consumer group, so offsets are tracked and work can be split across
  instances;
* manual offset commits after the database transaction, so a crash mid-message
  replays it rather than skipping it;
* a processed_events row written in that same transaction, so a replay is a
  no-op instead of duplicate work.
"""

import json
import logging
import os
import signal
import string
import sys

from app import Post, ProcessedEvent, app, db
from kafka import KafkaConsumer
from sqlalchemy.exc import SQLAlchemyError
from strsimpy.cosine import Cosine

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(levelname)s %(asctime)s %(name)s %(message)s",
)
logger = logging.getLogger("consumer")

KAFKA_BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
GROUP_ID = os.getenv("KAFKA_GROUP_ID", "related-service")
TOPICS = ["post_created", "post_updated", "post_deleted"]

# Loaded once, and deliberately not nltk.corpus.stopwords: that needs a
# downloaded corpus at import time, which turns a missing data file into an
# import crash. This is the subset that matters for comparing titles.
STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "if", "in",
    "into", "is", "it", "no", "not", "of", "on", "or", "such", "that", "the",
    "their", "then", "there", "these", "they", "this", "to", "was", "will",
    "with", "from", "how", "what", "when", "where", "who", "why",
}

_running = True


def _stop(signum, _frame):
    global _running  # noqa: PLW0603
    logger.info("signal %s received, stopping after this message", signum)
    _running = False


def clean(text):
    text = "".join(ch for ch in (text or "") if ch not in string.punctuation).lower()
    return " ".join(word for word in text.split() if word not in STOPWORDS)


def similarity(left, right):
    left, right = clean(left), clean(right)
    # Cosine(2) compares character 2-grams, so anything shorter than two
    # characters has an empty profile and the library raises instead of
    # returning zero.
    if len(left) < 2 or len(right) < 2:
        return 0.0
    cosine = Cosine(2)
    return cosine.similarity_profiles(cosine.get_profile(left), cosine.get_profile(right))


def apply_created(payload):
    if db.session.get(Post, payload["id"]) is not None:
        return

    related = {}
    for other in Post.query.all():
        score = similarity(other.title, payload["title"])
        related[other.slug] = score
        # Reassigned, not mutated. SQLAlchemy tracks attribute assignment, not
        # in-place changes to a dict, so `other.related[k] = v` alone is never
        # written back. That is why the old delete path silently did nothing.
        other.related = {**(other.related or {}), payload["slug"]: score}

    db.session.add(
        Post(
            id=payload["id"],
            title=payload["title"],
            image=payload.get("image"),
            body=payload.get("body"),
            slug=payload["slug"],
            related=related,
        )
    )


def apply_updated(payload):
    post = db.session.get(Post, payload["id"])
    if post is None:
        # The create may not have arrived yet. Treat an update for an unknown
        # post as a create rather than dropping it.
        apply_created(payload)
        return

    # str.strip(). The original called .stripe(), which is not a string method,
    # so every update raised AttributeError and this path never ran.
    title_changed = payload["title"].strip() != (post.title or "").strip()

    post.title = payload["title"]
    post.image = payload.get("image")
    post.body = payload.get("body")
    post.slug = payload["slug"]

    if not title_changed:
        return

    related = {}
    for other in Post.query.filter(Post.id != post.id).all():
        score = similarity(other.title, post.title)
        related[other.slug] = score
        other.related = {**(other.related or {}), post.slug: score}
    post.related = related


def apply_deleted(payload):
    slug = payload["slug"]
    post = Post.query.filter_by(slug=slug).first()
    if post is not None:
        db.session.delete(post)

    for other in Post.query.filter(Post.slug != slug).all():
        current = other.related or {}
        if slug in current:
            other.related = {k: v for k, v in current.items() if k != slug}


HANDLERS = {
    "post_created": apply_created,
    "post_updated": apply_updated,
    "post_deleted": apply_deleted,
}


def event_id_for(message):
    """Stable identity for a delivery.

    The relay sends the outbox row id as a header, so a republish of the same
    outbox row is recognised even though it lands at a new offset. The fallback
    identifies the record itself.
    """
    headers = dict(message.headers or [])
    raw = headers.get("event_id")
    if raw:
        return raw.decode()
    return f"{message.topic}:{message.partition}:{message.offset}"


def handle(message):
    """Apply one message. Returns True when the offset may be committed."""
    event_id = event_id_for(message)

    with app.app_context():
        try:
            if db.session.get(ProcessedEvent, event_id) is not None:
                logger.info("event %s already applied, skipping", event_id)
                return True

            HANDLERS[message.topic](message.value)
            db.session.add(ProcessedEvent(event_id=event_id, topic=message.topic))
            db.session.commit()
            logger.info("applied %s for %s", message.topic, message.value.get("slug"))
            return True
        except (SQLAlchemyError, KeyError, TypeError):
            db.session.rollback()
            # Not committing the offset means this message is redelivered.
            logger.exception("failed to apply %s (event %s)", message.topic, event_id)
            return False


def build_consumer():
    return KafkaConsumer(
        *TOPICS,
        bootstrap_servers=KAFKA_BOOTSTRAP.split(","),
        group_id=GROUP_ID,
        # Committed after the database transaction, not before. Automatic
        # commits can acknowledge a message the service has not applied yet, so
        # a crash loses it permanently.
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        value_deserializer=lambda v: json.loads(v.decode("utf-8")),
        consumer_timeout_ms=1000,
    )


def main():
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    consumer = build_consumer()
    logger.info("consuming %s as group %s", TOPICS, GROUP_ID)

    while _running:
        for message in consumer:
            if handle(message):
                consumer.commit()
            if not _running:
                break

    consumer.close()
    logger.info("consumer stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
