from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.response import Response

from .models import OutboxEvent, Post
from .serializers import PostSerializer

TOPIC_CREATED = "post_created"
TOPIC_UPDATED = "post_updated"
TOPIC_DELETED = "post_deleted"


def record_event(topic, key, payload):
    """Queue an event for the relay to publish.

    Call this inside the same transaction as the write it describes. Nothing
    here touches Kafka: the request path only writes to its own database, so it
    cannot half-succeed.
    """
    return OutboxEvent.objects.create(topic=topic, key=key, payload=payload)


class PostViewSet(viewsets.ViewSet):
    """Posts, with every mutation recorded in the outbox atomically."""

    lookup_field = "slug"

    def list(self, request):
        posts = Post.objects.all()
        return Response(PostSerializer(posts, many=True).data)

    def retrieve(self, request, slug=None):
        post = get_object_or_404(Post, slug=slug)
        return Response(PostSerializer(post).data)

    def create(self, request):
        serializer = PostSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # One transaction covering the post and the event. Previously the event
        # was fired from a detached thread whose result nobody read, so a
        # publish failure was invisible and the two services silently diverged.
        with transaction.atomic():
            post = serializer.save()
            record_event(TOPIC_CREATED, post.slug, PostSerializer(post).data)

        return Response(PostSerializer(post).data, status=status.HTTP_201_CREATED)

    def update(self, request, slug=None):
        post = get_object_or_404(Post, slug=slug)
        serializer = PostSerializer(instance=post, data=request.data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            post = serializer.save()
            record_event(TOPIC_UPDATED, post.slug, PostSerializer(post).data)

        return Response(PostSerializer(post).data, status=status.HTTP_200_OK)

    def destroy(self, request, slug=None):
        post = get_object_or_404(Post, slug=slug)

        with transaction.atomic():
            payload = {"id": post.id, "slug": post.slug}
            post.delete()
            record_event(TOPIC_DELETED, payload["slug"], payload)

        return Response(status=status.HTTP_204_NO_CONTENT)
