FROM python:3.9
ENV PYTHONUNBUFFERED 1
WORKDIR /app
COPY ./kafka_django/requirements.txt /app/requirements.txt
RUN pip install -r requirements.txt
RUN apt-get upgrade && apt-get update -y && apt-get install postgresql postgresql-contrib -y
COPY . /app
# RUN chmod +x ./kafka_django/wait-for-postgres.sh