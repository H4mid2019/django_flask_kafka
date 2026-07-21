from django.urls import path

from .views import PostViewSet

urlpatterns = [
    path("posts", PostViewSet.as_view({"get": "list", "post": "create"}), name="post-list"),
    path(
        "posts/<slug:slug>",
        PostViewSet.as_view({"get": "retrieve", "put": "update", "delete": "destroy"}),
        name="post-detail",
    ),
]
