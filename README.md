# django_flask_kafka

[![CI](https://github.com/H4mid2019/django_flask_kafka/actions/workflows/ci.yml/badge.svg)](https://github.com/H4mid2019/django_flask_kafka/actions/workflows/ci.yml)

Services that each own their database and stay consistent through Kafka events,
rather than by calling each other.

- **auth** (Go) issues RS256 tokens and publishes its public key. Owns users.
- **posts** (Django, DRF) owns posts. Every write records an event in an outbox
  table in the same transaction.
- **relay** reads that outbox and publishes to Kafka.
- **related** (Flask) consumes those events and maintains a table of related
  posts, scored by cosine similarity on titles. It never queries the posts
  service.

The point of the repo is what sits between them: making several databases and a
broker agree when no transaction spans them.

## Run it

```bash
docker compose up --build
```

Auth on `:8080`, posts on `:8000`, related on `:5000`. Reads are open; writes
need a token, so the examples below get one first (see Authentication).

```bash
# $token comes from the Authentication section below
AUTH="Authorization: Bearer $token"
JSON="content-type: application/json"

curl -X POST localhost:8000/api/posts -H "$AUTH" -H "$JSON" -d '{"title":"Kafka streams tutorial","body":"first","slug":"kafka-streams","image":""}'

curl -X POST localhost:8000/api/posts -H "$AUTH" -H "$JSON" -d '{"title":"Kafka streams advanced","body":"second","slug":"kafka-advanced","image":""}'

curl localhost:5000/posts
```

Real output a few seconds later, from a service whose database nobody wrote to
directly:

```json
[{"id":2,"slug":"kafka-advanced","title":"Kafka streams advanced",
  "related":{"kafka-streams":0.6521739130434784}},
 {"id":1,"slug":"kafka-streams","title":"Kafka streams tutorial",
  "related":{"kafka-advanced":0.6521739130434784}}]
```

## Why an outbox

Saving a post and publishing its event touch two systems with no shared
transaction. Doing them one after the other leaves a window where a crash makes
them disagree permanently: a post nobody was told about, or an event for a post
that was rolled back. The original code made it worse by publishing from a
detached thread whose result nobody read, so a failed publish was invisible.

So the request handler never talks to Kafka. It writes the post and an
`OutboxEvent` row in one database transaction, and the relay publishes from
there. An impossible problem, atomic write across two systems, becomes an
ordinary one: retry until the broker acknowledges.

The visible consequence is that the API keeps working when Kafka does not:

```bash
docker compose stop kafka

curl -X POST localhost:8000/api/posts -H "$AUTH" -H "$JSON" -d '{"title":"Written while Kafka was down","body":"x","slug":"during-outage","image":""}'
# 201 Created

docker compose exec posts-db psql -U posts -d posts -tAc \
  "SELECT count(*) FROM posts_outboxevent WHERE published_at IS NULL"
# 1

curl localhost:5000/posts/during-outage
# 404, correctly: the event has not been delivered yet

docker compose start kafka
# a few seconds pass

docker compose exec posts-db psql -U posts -d posts -tAc \
  "SELECT count(*) FROM posts_outboxevent WHERE published_at IS NULL"
# 0

curl localhost:5000/posts/during-outage
# the post, with related scores computed, and nobody replayed anything by hand
```

Those numbers are from an actual run, not an illustration.

## Why the consumer deduplicates

The outbox buys atomicity at the cost of exactly-once delivery. The relay can
publish successfully and die before recording that it did, then publish again on
restart. A consumer group rebalance replays too, and so does a consumer that
applies a message and crashes before committing its offset.

So the consumer writes a `processed_events` row in the same transaction as the
effect. Applying an event twice is then a no-op rather than duplicate work.
Rewinding the group to the beginning demonstrates it:

```bash
docker compose exec kafka /opt/kafka/bin/kafka-consumer-groups.sh \
  --bootstrap-server localhost:9092 --group related-service \
  --reset-offsets --to-earliest --all-topics --execute
```

Every event is redelivered from offset 0. Measured after doing exactly that:
posts unchanged at 2, processed events unchanged at 5, and 5 log lines reading
`already applied, skipping`.

## Authentication without a per-request hop

The Go service signs access tokens and serves its public key at
`/.well-known/jwks.json`. The Python services fetch that once, cache it, and
verify every request in-process.

They never call the auth service to check a token. Doing so would put a network
hop on the hot path of every request and make auth a hard dependency of both:
auth restarts for a deploy, everything stops serving. Signature verification
needs only the public key, so there is no reason to pay that.

```bash
CREDS='{"email":"you@example.com","password":"a-long-enough-password"}'
JSON='content-type: application/json'

curl -X POST localhost:8080/register -H "$JSON" -d "$CREDS"

token=$(curl -s -X POST localhost:8080/login -H "$JSON" -d "$CREDS" | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')

curl -X POST localhost:8000/api/posts -H "Authorization: Bearer $token" -H "$JSON" -d '{"title":"Written with a Go-issued token","body":"x","slug":"with-token","image":""}'
# 201. Django verified that token locally; the auth service was not contacted.
```

Reads stay open, writes need a token. Without one the answer is 401, not 403:
the caller did not say who they are, which is different from not being allowed.

## Revocation, which local verification cannot do alone

A signed token stays valid until it expires. Nothing can withdraw it, so the
only way to stop honouring one is to tell every service. That is a broadcast,
and Kafka is already here.

`POST /revoke` writes the revocation and an outbox row in one transaction, and
the relay publishes it. Each Python service consumes the topic **under its own
consumer group**, so each receives every revocation. Sharing one group would
split them and each service would see only some.

Measured on a running stack:

```
POST /revoke                                  -> 202 Accepted
posts service refused the token after ~4s
posts database:    1 row in posts_revokedtoken
related database:  1 row in revoked_tokens
```

The denylist lives in each service's database, not in process memory, because
both run several gunicorn workers. An in-memory list would only reach whichever
worker consumed the event, so the same token would be rejected by one worker and
accepted by the next. Rows are dropped once the token would have expired anyway,
so the table stays bounded.

Access tokens last 15 minutes, which bounds the damage if a service misses the
message entirely. Refresh tokens are stored hashed and rotate on use: redeeming
one revokes it, so a stolen refresh token replayed after the real client has
already refreshed fails instead of minting a second session.

## Design decisions

**Offsets are committed manually, after the database transaction.**
`enable_auto_commit` acknowledges on a timer, which can mark a message done
before the service has applied it, so a crash loses it silently. Committing
afterwards means a crash replays instead, which is safe because of the
deduplication above.

**Events are keyed by slug.** Kafka orders within a partition and the key selects
the partition, so one post's create, update and delete cannot overtake each
other. Ordering between different posts does not matter here.

**The relay claims rows with `FOR UPDATE SKIP LOCKED`**, so more than one relay
can run without duplicating work or blocking.

**The producer waits for acknowledgement.** `acks=all` plus an explicit
`flush()`, with every future resolved before the outbox row is marked published.
`send()` is asynchronous; the original called it and returned, so a message
could be lost before leaving the process with no error anywhere.

**`max_in_flight_requests_per_connection=1`**, because retries can otherwise
reorder messages, undoing the per-key ordering the slug key exists to provide.

**The relay is its own process.** Publishing needs to retry for as long as the
broker is down, and an HTTP handler cannot wait that long. Separating it also
keeps the API accepting writes during an outage.

## Bugs fixed from the original

Each is now covered by a test:

- `consumer.py` called `data["title"].stripe(" ")`. There is no `str.stripe`, so
  every `post_updated` event raised `AttributeError` and the update path had
  never run.
- The delete handler removed back-references with `post.related.pop(...)`.
  SQLAlchemy does not detect in-place mutation of a JSON column, and there was
  no commit after the loop, so the write silently did nothing.
- The producer built a new `KafkaProducer` per message, called `send()`, and
  returned without flushing, so messages could be lost before transmission.
  Exceptions were swallowed by a bare `except` whose return value nobody read.
- Consumers had no `group_id`, so offsets were never tracked and the service
  could not be scaled or restarted safely.
- `bootstrap_servers` and the Postgres URL, credentials included, were hardcoded
  to `127.0.0.1`, which is why the old compose files needed `network_mode: host`.
- `DEBUG = True`, `ALLOWED_HOSTS = ['*']`, and a committed `SECRET_KEY` fallback.
- `create_post` loaded the whole table and committed inside the loop, so each
  event cost O(N) commits.
- Three compose files and a `wait-for-postgres.sh` that polled a port. Compose
  healthchecks with `depends_on.condition` express this directly, and unlike the
  script they distinguish an open socket from a database ready for queries.

## Tests

```bash
cd services/auth    && go test ./...             # tokens, JWKS, argon2id
cd services/posts   && python manage.py test    # outbox atomicity, auth, revocation
cd services/related && python -m pytest tests/  # handlers, similarity, idempotency
```

The handler tests run on SQLite and never open a socket, so they need neither
Kafka nor Postgres. CI runs those, plus an end to end job that brings the compose
stack up, creates a post in one service, waits for it to appear in the other,
then stops the broker and checks the event queues and drains.

## Layout

```
services/auth/      Go: issues RS256 tokens, serves JWKS, publishes revocations
services/posts/     Django API, Post and OutboxEvent written in one transaction
services/relay/     reads the outbox, publishes to Kafka, marks rows published
services/related/   Flask API and Kafka consumer, owns the related-posts database
docker-compose.yml  both databases, Kafka in KRaft mode, all three services
```

## Not implemented

No key rotation: the signing key is loaded once, and rotating it would need the
JWKS to serve both keys during the overlap. No roles or scopes, so a valid token
authorises any write. No rate limiting on login. No dead letter queue, so a
message that fails repeatedly blocks its partition.
No schema registry, so the payload contract is a convention rather than
something enforced. No outbox cleanup, so published rows accumulate. Similarity
is recomputed against every post on each event, which is fine for hundreds and
not for millions.
