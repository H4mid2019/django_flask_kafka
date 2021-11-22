FROM python:3.9
ENV PYTHONUNBUFFERED 1
WORKDIR /app
COPY ./related_flask/requirements.txt /app/requirements.txt
RUN pip install -U pip
RUN pip install -r requirements.txt
RUN (echo "import nltk"; echo "nltk.download('stopwords')") | python
COPY . /app