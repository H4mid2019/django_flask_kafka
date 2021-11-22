from django.db import models


class Post(models.Model):
    title = models.CharField(max_length=200)
    image = models.CharField(max_length=250)
    body = models.TextField(max_length=1500)
    slug = models.CharField(max_length=200, unique=True)

    def __str__(self) -> str:
        return self.title
        
