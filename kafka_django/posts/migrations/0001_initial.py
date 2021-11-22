# Generated by Django 3.2.9 on 2021-11-11 19:24

from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
    ]

    operations = [
        migrations.CreateModel(
            name='Post',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title', models.CharField(max_length=200)),
                ('image', models.CharField(max_length=250)),
                ('body', models.TextField(max_length=1500)),
                ('slug', models.CharField(max_length=200, unique=True)),
            ],
        ),
    ]