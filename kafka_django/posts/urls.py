from django.urls import path

from .views import PostViewSet

urlpatterns = [
    path('posts', PostViewSet.as_view({
        'get': 'list',
        'post': 'create'
    })),
    path('post/<str:slug>', PostViewSet.as_view({
        'get': 'retrieve',
        'put': 'update',
        'delete': 'destroy'
    })),
]