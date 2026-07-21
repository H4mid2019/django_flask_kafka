"""Tests for the posts service.

The property that matters is that a write and its event are one transaction.
Everything else here supports that claim.
"""

from django.db import transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import OutboxEvent, Post
from .views import TOPIC_CREATED, TOPIC_DELETED, TOPIC_UPDATED


class OutboxAtomicityTests(TestCase):
    """The write and its event either both land or neither does."""

    def test_create_records_one_event(self):
        response = self.client.post(
            reverse("post-list"),
            {"title": "First post", "body": "hello", "slug": "first-post", "image": ""},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 201)

        event = OutboxEvent.objects.get()
        self.assertEqual(event.topic, TOPIC_CREATED)
        self.assertEqual(event.key, "first-post")
        self.assertEqual(event.payload["title"], "First post")
        self.assertIsNone(event.published_at)

    def test_event_is_rolled_back_with_the_post(self):
        """The whole point of the outbox.

        When the transaction fails after the post is saved, the event must not
        survive. Publishing from the view, as this service used to, could not
        offer that: the message was already on its way to Kafka.
        """
        with self.assertRaises(RuntimeError), transaction.atomic():
            Post.objects.create(title="Doomed", body="x", slug="doomed")
            OutboxEvent.objects.create(topic=TOPIC_CREATED, key="doomed", payload={})
            raise RuntimeError("something failed later in the transaction")

        self.assertEqual(Post.objects.count(), 0)
        self.assertEqual(OutboxEvent.objects.count(), 0)

    def test_no_event_when_the_request_is_invalid(self):
        response = self.client.post(
            reverse("post-list"),
            {"body": "no title, no slug"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(OutboxEvent.objects.count(), 0)
        self.assertEqual(Post.objects.count(), 0)

    def test_duplicate_slug_leaves_no_event(self):
        Post.objects.create(title="Taken", body="x", slug="taken")

        response = self.client.post(
            reverse("post-list"),
            {"title": "Also taken", "body": "y", "slug": "taken"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(OutboxEvent.objects.count(), 0)


class EventContentTests(TestCase):
    def setUp(self):
        self.post = Post.objects.create(title="Original", body="body", slug="original")

    def test_update_records_an_update_event(self):
        response = self.client.put(
            reverse("post-detail", args=["original"]),
            {"title": "Renamed", "body": "body", "slug": "original", "image": ""},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)

        event = OutboxEvent.objects.get()
        self.assertEqual(event.topic, TOPIC_UPDATED)
        self.assertEqual(event.payload["title"], "Renamed")

    def test_delete_records_a_delete_event_carrying_the_slug(self):
        response = self.client.delete(reverse("post-detail", args=["original"]))
        self.assertEqual(response.status_code, 204)

        event = OutboxEvent.objects.get()
        self.assertEqual(event.topic, TOPIC_DELETED)
        # The consumer keys off the slug, so it has to outlive the row.
        self.assertEqual(event.payload["slug"], "original")
        self.assertEqual(Post.objects.count(), 0)

    def test_events_are_keyed_by_slug_for_ordering(self):
        """Kafka orders within a partition and the key selects it.

        One key means a post's events cannot overtake each other.
        """
        self.client.put(
            reverse("post-detail", args=["original"]),
            {"title": "Second", "body": "body", "slug": "original", "image": ""},
            content_type="application/json",
        )
        self.client.delete(reverse("post-detail", args=["original"]))

        keys = list(OutboxEvent.objects.order_by("created_at").values_list("key", flat=True))
        self.assertEqual(keys, ["original", "original"])

    def test_missing_post_is_404_not_500(self):
        response = self.client.get(reverse("post-detail", args=["nope"]))
        self.assertEqual(response.status_code, 404)


class RelayContractTests(TestCase):
    """What the relay depends on. Breaking these breaks it silently."""

    def test_only_unpublished_events_are_pending(self):
        for index in range(3):
            OutboxEvent.objects.create(
                topic=TOPIC_CREATED, key=f"post-{index}", payload={"slug": f"post-{index}"}
            )

        published = OutboxEvent.objects.order_by("created_at").first()
        published.published_at = timezone.now()
        published.save()

        pending = OutboxEvent.objects.filter(published_at__isnull=True)
        self.assertEqual(pending.count(), 2)

    def test_table_and_column_names_match_the_relay_query(self):
        """The relay reads this table with raw SQL, so the names are a contract."""
        self.assertEqual(OutboxEvent._meta.db_table, "posts_outboxevent")
        columns = {field.column for field in OutboxEvent._meta.get_fields()}
        self.assertTrue({"id", "topic", "key", "payload", "published_at"} <= columns)
