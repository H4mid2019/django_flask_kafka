# Django_Flask_Kafka


_**Tech Stack**_

Django, Flask, Docker, PostgreSQL, Kafka

## The Concept

It demonstrates two separate apps. One of them is Django, and the other one is Flask, which they can communicate 
by a message broker (Kafka). For instance, here, the concept is that Django is the backend which the user creates, 
updates, or deletes posts via its endpoint API, then the Flask app calculates and finds out the related posts, and 
it saves them in its database separately. To retrieve the posts with their related posts, 
you have to call the Flask endpoint API.


## Prerequisites

- _These TCP ports must be free in the Host machine._
**2181, 9092, 5434, 5433, 8000, 5000**

- Docker and docker-compose must be installed.

## Setup

First, convert `.env.example` to `.env` then, In the main folder for the first time, run `docker-compose up --build`.
After that, you can run it only by `docker-compose up`.


### Endpoints:

- 127.0.0.1:8000/api/posts :

_with GET method: retrieves all posts_

_with POST method: makes a new post, the requests must be included a JSON formatted body with title, slug, body, image_

- 127.0.0.1:8000/api/post/{slug} :

_with GET method: retrieves the single post with the {slug}_

_with DELETE method: deletes the post_

_with PUT method: it updates the post_

- 127.0.0.1:5000/posts :

_GET method: It returns all posts with the related attribute for every post_
