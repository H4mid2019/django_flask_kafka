"""Tests for the posts service.

The property that matters is that a write and its event are one transaction.
Everything else here supports that claim.
"""

from django.db import transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import jwtauth
from .models import OutboxEvent, Post, RevokedToken
from .testauth import FakeJWKClient, TokenFactory
from .views import TOPIC_CREATED, TOPIC_DELETED, TOPIC_UPDATED


class AuthenticatedTestCase(TestCase):
    """Signs requests with a real token verified through the real code path."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.tokens = TokenFactory()

    def setUp(self):
        super().setUp()
        # Only the key fetch is replaced. Signature, issuer, audience and expiry
        # are all still checked by jwt.decode.
        jwtauth._jwk_client = FakeJWKClient(self.tokens.public_key)
        self.auth = self.tokens.auth_header()

    def tearDown(self):
        jwtauth._jwk_client = None
        super().tearDown()


class OutboxAtomicityTests(AuthenticatedTestCase):
    """The write and its event either both land or neither does."""

    def test_create_records_one_event(self):
        response = self.client.post(
            reverse("post-list"),
            {"title": "First post", "body": "hello", "slug": "first-post", "image": ""},
            content_type="application/json",
            **self.auth,
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
            **self.auth,
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
            **self.auth,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(OutboxEvent.objects.count(), 0)


class EventContentTests(AuthenticatedTestCase):
    def setUp(self):
        super().setUp()
        self.post = Post.objects.create(title="Original", body="body", slug="original")

    def test_update_records_an_update_event(self):
        response = self.client.put(
            reverse("post-detail", args=["original"]),
            {"title": "Renamed", "body": "body", "slug": "original", "image": ""},
            content_type="application/json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 200)

        event = OutboxEvent.objects.get()
        self.assertEqual(event.topic, TOPIC_UPDATED)
        self.assertEqual(event.payload["title"], "Renamed")

    def test_delete_records_a_delete_event_carrying_the_slug(self):
        response = self.client.delete(reverse("post-detail", args=["original"]), **self.auth)
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
            **self.auth,
        )
        self.client.delete(reverse("post-detail", args=["original"]), **self.auth)

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


class AuthenticationTests(AuthenticatedTestCase):
    """Writes need a valid token. Reads do not."""

    def test_reads_are_open(self):
        self.assertEqual(self.client.get(reverse("post-list")).status_code, 200)

    def test_write_without_a_token_is_rejected(self):
        response = self.client.post(
            reverse("post-list"),
            {"title": "No token", "body": "x", "slug": "no-token"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(Post.objects.count(), 0)
        self.assertEqual(OutboxEvent.objects.count(), 0)

    def test_garbage_token_is_rejected(self):
        response = self.client.post(
            reverse("post-list"),
            {"title": "Bad token", "body": "x", "slug": "bad-token"},
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer not-a-token",
        )
        self.assertEqual(response.status_code, 401)

    def test_token_signed_by_another_key_is_rejected(self):
        """A token from a key this service does not trust must not work."""
        stranger = TokenFactory()
        response = self.client.post(
            reverse("post-list"),
            {"title": "Forged", "body": "x", "slug": "forged"},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {stranger.token()}",
        )
        self.assertEqual(response.status_code, 401)

    def test_expired_token_is_rejected(self):
        import datetime as dt

        past = dt.datetime.now(dt.UTC) - dt.timedelta(hours=2)
        header = {
            "HTTP_AUTHORIZATION": f"Bearer {self.tokens.token(exp=past, iat=past)}"
        }
        response = self.client.post(
            reverse("post-list"),
            {"title": "Expired", "body": "x", "slug": "expired"},
            content_type="application/json",
            **header,
        )
        self.assertEqual(response.status_code, 401)

    def test_token_for_another_audience_is_rejected(self):
        response = self.client.post(
            reverse("post-list"),
            {"title": "Wrong audience", "body": "x", "slug": "wrong-aud"},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.tokens.token(aud='some-other-service')}",
        )
        self.assertEqual(response.status_code, 401)


class RevocationTests(AuthenticatedTestCase):
    """A signed token cannot be withdrawn, so it is refused locally instead."""

    def test_revoked_token_is_refused(self):
        import datetime as dt
        import uuid

        token_id = str(uuid.uuid4())
        header = {"HTTP_AUTHORIZATION": f"Bearer {self.tokens.token(token_id=token_id)}"}

        # Works before revocation.
        first = self.client.post(
            reverse("post-list"),
            {"title": "Before", "body": "x", "slug": "before-revoke"},
            content_type="application/json",
            **header,
        )
        self.assertEqual(first.status_code, 201)

        # What the token_revoked consumer writes.
        RevokedToken.objects.create(
            token_id=token_id,
            user_id="someone",
            expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(hours=1),
        )

        second = self.client.post(
            reverse("post-list"),
            {"title": "After", "body": "x", "slug": "after-revoke"},
            content_type="application/json",
            **header,
        )
        self.assertEqual(second.status_code, 401)
        self.assertFalse(Post.objects.filter(slug="after-revoke").exists())

    def test_revoking_one_token_does_not_affect_another(self):
        import datetime as dt
        import uuid

        RevokedToken.objects.create(
            token_id=str(uuid.uuid4()),
            user_id="someone",
            expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(hours=1),
        )

        response = self.client.post(
            reverse("post-list"),
            {"title": "Still fine", "body": "x", "slug": "still-fine"},
            content_type="application/json",
            **self.auth,
        )
        self.assertEqual(response.status_code, 201)

    def test_expired_revocations_are_purged(self):
        """The denylist only needs an entry until the token expires anyway."""
        import datetime as dt
        import uuid

        RevokedToken.objects.create(
            token_id=str(uuid.uuid4()),
            user_id="someone",
            expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1),
        )
        RevokedToken.objects.create(
            token_id=str(uuid.uuid4()),
            user_id="someone",
            expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(hours=1),
        )

        jwtauth.purge_expired_revocations()
        self.assertEqual(RevokedToken.objects.count(), 1)
