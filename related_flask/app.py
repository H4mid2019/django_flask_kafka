from flask import Flask, jsonify
from dataclasses import dataclass
from flask_cors import CORS
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from sqlalchemy.dialects.postgresql import JSON

app = Flask(__name__)
 
app.config['SQLALCHEMY_DATABASE_URI'] = "postgresql://related_flask:related_flask@127.0.0.1:5434/related_flask"
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

CORS(app)

db = SQLAlchemy(app)
migrate = Migrate(app, db)


@dataclass
class Post(db.Model):
    id: int
    title: str
    image: str
    body: str
    slug: str
    related: dict

    id = db.Column(db.Integer, primary_key=True, autoincrement=False)
    title = db.Column(db.String(200))
    image = db.Column(db.String(200))
    body = db.Column(db.String(1500))
    slug = db.Column(db.String(250))
    related = db.Column(JSON)

    def __repr__(self) -> str:
        return f"<title={self.title}>"


@app.route('/posts')
def posts():
    return jsonify(Post.query.all())
 

@app.route('/')
def index():
    return "Hello"


if __name__ == '__main__':
    app.run(debug=True, port=5000, host='0.0.0.0')