import uuid

from django.db import models


class Post(models.Model):
    title = models.CharField(max_length=200)
    image = models.CharField(max_length=250, blank=True)
    body = models.TextField(max_length=1500)
    slug = models.SlugField(max_length=200, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self) -> str:
        return self.title


class OutboxEvent(models.Model):
    """An event waiting to be published to Kafka.

    This table is why the service never talks to Kafka from a request handler.
    Saving the post and publishing the event touch two systems with no shared
    transaction, so doing them one after another leaves a window where a crash
    makes them disagree permanently: a post nobody was told about, or an event
    for a post that was rolled back.

    Writing the row and the event in one database transaction closes that
    window. The relay reads this table and publishes, which turns an impossible
    problem (atomic write across two systems) into an ordinary one (retry until
    the broker acknowledges).

    The cost is that delivery becomes at-least-once: the relay can publish and
    die before recording that it did, then republish on restart. Consumers have
    to be idempotent, which is what processed_events on the related service is
    for.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    topic = models.CharField(max_length=100)
    # Kafka orders within a partition, and the key selects the partition. Keying
    # on the slug keeps one post's create, update and delete in order relative
    # to each other, which is the ordering that matters here.
    key = models.CharField(max_length=200)
    payload = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            # The relay's only query: unpublished rows, oldest first.
            models.Index(
                fields=["created_at"],
                condition=models.Q(published_at__isnull=True),
                name="outbox_unpublished_idx",
            ),
        ]

    def __str__(self) -> str:
        state = "published" if self.published_at else "pending"
        return f"{self.topic}/{self.key} ({state})"


class RevokedToken(models.Model):
    """Access tokens refused before their expiry.

    Kept in the database rather than in process memory because this service runs
    several gunicorn workers. An in-memory denylist would only reach whichever
    worker happened to consume the event, so the same revoked token would be
    rejected by one worker and accepted by the next.

    Written by the token_revoked consumer, read on every authenticated request.
    Rows are removed once the token would have expired anyway; see
    purge_expired_revocations.
    """

    token_id = models.CharField(max_length=64, primary_key=True)
    user_id = models.CharField(max_length=64)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["expires_at"], name="revoked_expires_idx")]

    @classmethod
    def is_revoked(cls, token_id: str) -> bool:
        if not token_id:
            return False
        return cls.objects.filter(token_id=token_id).exists()

    def __str__(self) -> str:
        return f"revoked {self.token_id}"
