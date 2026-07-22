"""Applies token_revoked events to the local denylist.

A separate consumer group from any other service, because this is a broadcast:
every service needs every revocation, unlike the posts events where one consumer
group shares the work.
"""

import json
import logging
import signal

from django.core.management.base import BaseCommand
from django.utils.dateparse import parse_datetime
from kafka import KafkaConsumer

from posts.jwtauth import purge_expired_revocations
from posts.models import RevokedToken

logger = logging.getLogger(__name__)

TOPIC = "token_revoked"


class Command(BaseCommand):
    help = "Consume token_revoked events into the local denylist"

    def handle(self, *args, **options):
        from django.conf import settings

        self.running = True
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)

        consumer = KafkaConsumer(
            TOPIC,
            bootstrap_servers=settings.KAFKA_BOOTSTRAP_SERVERS.split(","),
            # Unique per service. Two services sharing a group would each see
            # only some revocations, which is exactly wrong for a broadcast.
            group_id=settings.REVOCATION_GROUP_ID,
            enable_auto_commit=False,
            auto_offset_reset="earliest",
            value_deserializer=lambda v: json.loads(v.decode("utf-8")),
            consumer_timeout_ms=1000,
        )
        logger.info("consuming %s as group %s", TOPIC, settings.REVOCATION_GROUP_ID)

        while self.running:
            for message in consumer:
                if self._apply(message.value):
                    consumer.commit()
                if not self.running:
                    break
            purge_expired_revocations()

        consumer.close()
        logger.info("revocation consumer stopped")

    def _apply(self, payload):
        try:
            expires_at = parse_datetime(payload["expires_at"])
            # update_or_create, not create: the relay guarantees at-least-once
            # delivery, so the same revocation can arrive twice and must not
            # raise on the second.
            RevokedToken.objects.update_or_create(
                token_id=payload["token_id"],
                defaults={"user_id": payload["user_id"], "expires_at": expires_at},
            )
            logger.info("token %s denied locally", payload["token_id"])
            return True
        except (KeyError, TypeError, ValueError):
            logger.exception("could not apply revocation: %s", payload)
            # Committing anyway: a malformed payload will never parse, so
            # replaying it forever would block the partition.
            return True

    def _stop(self, signum, frame):
        logger.info("signal %s received", signum)
        self.running = False
