"""Related-posts service.

Owns its own database. It never queries the posts service; everything it knows
arrives as events, which is the point of the split. It is eventually consistent
with posts by design.
"""

import os
from datetime import UTC, datetime

from flask import Flask, jsonify
from flask_cors import CORS
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import JSONB

# JSONB in Postgres, plain JSON when the tests run on SQLite, which has no
# JSONB. The variant keeps one model definition rather than branching on the
# dialect at import time.
RelatedMap = JSONB().with_variant(JSON(), "sqlite")

db = SQLAlchemy()
migrate = Migrate()


class Post(db.Model):
    __tablename__ = "post"

    id = db.Column(db.Integer, primary_key=True, autoincrement=False)
    title = db.Column(db.String(200), nullable=False)
    image = db.Column(db.String(250))
    body = db.Column(db.String(1500))
    slug = db.Column(db.String(250), nullable=False, unique=True, index=True)
    # JSONB rather than JSON in Postgres: queryable, indexable, comparable.
    related = db.Column(RelatedMap, nullable=False, default=dict)
    add_date = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(UTC))

    def to_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "image": self.image,
            "body": self.body,
            "slug": self.slug,
            "related": self.related or {},
            "add_date": self.add_date.isoformat() if self.add_date else None,
        }


class RevokedToken(db.Model):
    """Access tokens refused before their expiry.

    In the database rather than process memory because this service runs several
    gunicorn workers plus a consumer process. An in-memory denylist would only
    reach whichever process consumed the event, so the same token would be
    rejected by one worker and accepted by the next.
    """

    __tablename__ = "revoked_tokens"

    token_id = db.Column(db.String(64), primary_key=True)
    user_id = db.Column(db.String(64), nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)
    revoked_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(UTC))


class ProcessedEvent(db.Model):
    """Every event id this service has already applied.

    The relay guarantees at-least-once delivery, so the same event can arrive
    twice: after a relay restart, after a consumer group rebalance, or when the
    consumer applies a message and dies before committing its offset. Without
    this table a redelivered post_created would recompute and rewrite
    similarities, and a redelivered post_deleted would fail on a row that is
    already gone.

    Inserting the id in the same transaction as the effect is what makes
    applying an event idempotent: either both land or neither does.
    """

    __tablename__ = "processed_events"

    event_id = db.Column(db.String(64), primary_key=True)
    topic = db.Column(db.String(100), nullable=False)
    processed_at = db.Column(db.DateTime, nullable=False, default=lambda: datetime.now(UTC))


def create_app():
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv(
        "DATABASE_URL", "postgresql://related:related@localhost:5432/related"
    )
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

    CORS(app)
    db.init_app(app)
    migrate.init_app(app, db)

    @app.route("/health")
    def health():
        return jsonify({"status": "ok"})

    @app.route("/posts")
    def posts():
        # Bounded. Returning the whole table was fine with ten rows and is not
        # something this endpoint should ever be asked to do.
        limit = min(int(os.getenv("PAGE_SIZE", "100")), 500)
        rows = Post.query.order_by(Post.id.desc()).limit(limit).all()
        return jsonify([row.to_dict() for row in rows])

    @app.route("/posts/<slug>")
    def post_detail(slug):
        row = Post.query.filter_by(slug=slug).first()
        if row is None:
            return jsonify({"error": "not found"}), 404
        return jsonify(row.to_dict())

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
