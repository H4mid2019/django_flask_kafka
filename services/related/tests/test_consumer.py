"""Tests for the related-posts consumer.

No Kafka here. The handlers take a plain payload, so what the event bus does is
irrelevant to whether applying an event is correct. The Kafka wiring is covered
by the compose smoke test instead.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DATABASE_URL", "sqlite://")

from app import Post, ProcessedEvent, db  # noqa: E402
from app import app as flask_app
from consumer import (  # noqa: E402
    apply_created,
    apply_deleted,
    apply_updated,
    clean,
    similarity,
)


@pytest.fixture
def ctx():
    flask_app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite://"
    with flask_app.app_context():
        db.create_all()
        yield
        db.session.remove()
        db.drop_all()


def payload(post_id, title, slug):
    return {"id": post_id, "title": title, "slug": slug, "body": "body", "image": ""}


class TestSimilarity:
    def test_stopwords_and_punctuation_are_stripped(self):
        assert clean("The Quick, Brown Fox!") == "quick brown fox"

    def test_identical_titles_score_one(self):
        assert similarity("kafka streams", "kafka streams") == pytest.approx(1.0)

    def test_unrelated_titles_score_low(self):
        assert similarity("kafka streams", "baking bread") < 0.3

    def test_short_titles_do_not_raise(self):
        """Cosine(2) needs two characters; an empty profile used to blow up."""
        assert similarity("a", "b") == 0.0
        assert similarity("", "anything") == 0.0


class TestCreate:
    def test_creates_the_post(self, ctx):
        apply_created(payload(1, "Kafka basics", "kafka-basics"))
        db.session.commit()

        stored = db.session.get(Post, 1)
        assert stored.slug == "kafka-basics"

    def test_relates_new_post_to_existing_ones_both_ways(self, ctx):
        apply_created(payload(1, "Kafka streams tutorial", "kafka-streams"))
        db.session.commit()
        apply_created(payload(2, "Kafka streams advanced", "kafka-advanced"))
        db.session.commit()

        first = db.session.get(Post, 1)
        second = db.session.get(Post, 2)

        # The second knows about the first, and the first was updated to know
        # about the second. The back-reference is the half that silently failed
        # before, because the JSON column was mutated in place.
        assert "kafka-streams" in second.related
        assert "kafka-advanced" in first.related
        assert first.related["kafka-advanced"] > 0.5

    def test_applying_the_same_create_twice_is_a_no_op(self, ctx):
        apply_created(payload(1, "Kafka basics", "kafka-basics"))
        db.session.commit()
        apply_created(payload(1, "Kafka basics", "kafka-basics"))
        db.session.commit()

        assert Post.query.count() == 1


class TestUpdate:
    def test_update_applies(self, ctx):
        """The old code called .stripe(), so this path raised every time."""
        apply_created(payload(1, "Original title", "post-one"))
        db.session.commit()

        apply_updated(payload(1, "Completely different words", "post-one"))
        db.session.commit()

        assert db.session.get(Post, 1).title == "Completely different words"

    def test_update_recomputes_relations_when_the_title_changes(self, ctx):
        apply_created(payload(1, "Baking sourdough bread", "bread"))
        db.session.commit()
        apply_created(payload(2, "Kafka streams tutorial", "kafka"))
        db.session.commit()

        before = db.session.get(Post, 2).related["bread"]
        apply_updated(payload(1, "Kafka streams internals", "bread"))
        db.session.commit()

        after = db.session.get(Post, 2).related["bread"]
        assert after > before

    def test_update_for_an_unknown_post_creates_it(self, ctx):
        """Events can arrive out of order or after a compaction."""
        apply_updated(payload(9, "Arrived first", "orphan"))
        db.session.commit()

        assert db.session.get(Post, 9) is not None


class TestDelete:
    def test_delete_removes_the_post(self, ctx):
        apply_created(payload(1, "Going away", "goodbye"))
        db.session.commit()

        apply_deleted({"slug": "goodbye"})
        db.session.commit()

        assert Post.query.filter_by(slug="goodbye").first() is None

    def test_delete_removes_the_back_references(self, ctx):
        """This never worked. The JSON column was mutated with pop(), which
        SQLAlchemy does not detect, and there was no commit after the loop."""
        apply_created(payload(1, "Kafka streams tutorial", "kafka-one"))
        db.session.commit()
        apply_created(payload(2, "Kafka streams advanced", "kafka-two"))
        db.session.commit()

        assert "kafka-two" in db.session.get(Post, 1).related

        apply_deleted({"slug": "kafka-two"})
        db.session.commit()

        survivor = db.session.get(Post, 1)
        db.session.refresh(survivor)
        assert "kafka-two" not in survivor.related

    def test_deleting_an_unknown_slug_does_not_raise(self, ctx):
        apply_deleted({"slug": "never-existed"})
        db.session.commit()


class TestIdempotency:
    def test_processed_events_records_what_was_applied(self, ctx):
        db.session.add(ProcessedEvent(event_id="abc-123", topic="post_created"))
        db.session.commit()

        assert db.session.get(ProcessedEvent, "abc-123") is not None

    def test_event_id_is_the_primary_key(self, ctx):
        """Redelivery is detected by id, so it has to be unique."""
        from sqlalchemy.exc import IntegrityError

        db.session.add(ProcessedEvent(event_id="dup", topic="post_created"))
        db.session.commit()

        db.session.add(ProcessedEvent(event_id="dup", topic="post_created"))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()
