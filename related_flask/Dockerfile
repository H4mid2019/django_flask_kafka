FROM python:3.9
ENV PYTHONUNBUFFERED 1
WORKDIR /app
COPY ./related_flask/requirements.txt /app/requirements.txt
RUN pip install -U pip
RUN pip install -r requirements.txt
RUN apt-get upgrade && apt-get update -y && apt-get install postgresql postgresql-contrib -y
RUN (echo "import nltk"; echo "nltk.download('stopwords')") | python
COPY . /app