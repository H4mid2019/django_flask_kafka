from rest_framework import viewsets, status
from rest_framework.response import Response
from .producer import producer
from .models import Post
from .serializers import PostSerializer
from threading import Thread


class PostViewSet(viewsets.ViewSet):
    def list(self, request):
        posts = Post.objects.all()
        serializer = PostSerializer(posts, many=True)
        return Response(serializer.data)

    def create(self, request):
        serializer = PostSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        Thread(target=producer, args=('post_created', serializer.data)).start()
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    def retrieve(self, request, slug=None):
        try:
            post = Post.objects.get(slug=slug)
            serializer = PostSerializer(post)
            return Response(serializer.data)
        except Post.DoesNotExist:
            content = {'post': 'doesn\'t exist, please try again with correspond slug.'}
            return Response(content, status=status.HTTP_404_NOT_FOUND)

    def update(self, request, slug=None):
        try:
            post = Post.objects.get(slug=slug)
            serializer = PostSerializer(instance=post, data=request.data)
            serializer.is_valid(raise_exception=True)
            serializer.save()
            producer('post_updated', serializer.data)
            return Response(serializer.data, status=status.HTTP_202_ACCEPTED)
        except Post.DoesNotExist:
            content = {'post': 'doesn\'t exist, please try again with correspond slug.'}
            return Response(content, status=status.HTTP_404_NOT_FOUND)

    def destroy(self, request, slug=None):
        try:
            post = Post.objects.get(slug=slug)
            print("here", bool(post))
            post.delete()
            producer('post_deleted', {"slug": slug})
            return Response(status=status.HTTP_204_NO_CONTENT)
        except Post.DoesNotExist:
            content = {'post': 'does\'t exist, please try again with correspond slug.'}
            return Response(content, status=status.HTTP_404_NOT_FOUND)
