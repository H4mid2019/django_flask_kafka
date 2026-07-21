FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd --create-home --uid 1000 app && chown -R app:app /app
USER app

EXPOSE 5000
CMD ["gunicorn", "app:app", "-b", "0.0.0.0:5000", "-w", "2"]
