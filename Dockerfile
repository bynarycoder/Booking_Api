FROM python:3.12-slim

WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# SQLite lives on a mounted volume when one is available; otherwise set
# DATABASE_URL to a Postgres connection string.
ENV PORT=8000 SQLITE_PATH=/data/clinic.db
VOLUME ["/data"]
EXPOSE 8000

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
