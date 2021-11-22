from kafka import KafkaConsumer
import json
from app import Post, db
from strsimpy.cosine import Cosine
import string
from nltk.corpus import stopwords
from threading import Thread

stopwords = stopwords.words("english")


def clean_string(text):
    text = "".join([word for word in text if word not in string.punctuation])
    text = text.lower()
    text = " ".join([word for word in text.split() if word not in stopwords])
    return text


def similarity(s0, s1):
    s0, s1 = clean_string(s0), clean_string(s1)
    cosine = Cosine(2)
    p0 = cosine.get_profile(s0)
    p1 = cosine.get_profile(s1)
    return cosine.similarity_profiles(p0, p1)


def create_post(data):
    all_posts = Post.query.all()
    related = {}
    if not bool(Post.query.filter_by(id=data["id"]).first()):
        for p in all_posts:
            similarity_value = similarity(p.title, data["title"])
            related[p.slug] = similarity_value
            p.related[data["slug"]] = similarity_value
            db.session.commit()
        post = Post(id=data["id"], title=data["title"], image=data["image"], body=data["body"], slug=data["slug"],
                    related=related)
        db.session.add(post)
        db.session.commit()
    return


def update_post(data):
    post = Post.query.filter_by(id=data["id"]).first()
    if data["title"].stripe(" ") != post.title:
        all_posts = Post.query.all()
        related = {}
        for p in all_posts:
            similarity_value = similarity(p.title, post.title)
            related[p.slug] = similarity_value
            p.related[data["slug"]] = similarity_value
        post.title = data["title"]
        post.image = data["image"]
        post.body = data["body"]
        post.slug = data["slug"]
        post.related = related
        db.session.commit()
    return


def delete_post(data):
    post = Post.query.filter_by(slug=data["slug"]).first()
    db.session.delete(post)
    db.session.commit()
    del post
    posts = Post.query.all()
    for post in posts:
        post.related.pop(data["slug"], None)
    return


def create_consumer():
    consumer = KafkaConsumer('post_created', api_version=(0, 10),
                             auto_offset_reset='earliest',
                             enable_auto_commit=True,
                             value_deserializer=lambda x: json.loads(x.decode('utf-8'))
                             )
    for message in consumer:
        print("create post", message.value)
        create_post(message.value)
    return


def update_consumer():
    consumer = KafkaConsumer('post_updated', api_version=(0, 10))
    for message in consumer:
        print("update post", json.loads(message.value))
        update_post(json.loads(message.value))

    return


def delete_consumer():
    consumer = KafkaConsumer('post_deleted', api_version=(0, 10))
    for message in consumer:
        print("delete post", json.loads(message.value))
        delete_post(json.loads(message.value))
    return


targets = [create_consumer, update_consumer, delete_consumer]

for target in targets:
    Thread(target=target).start()

print("started")
