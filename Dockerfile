FROM python:3.12-slim

WORKDIR /app

# mongodb-database-tools (mongodump / mongorestore) for the OPTIONAL scheduled
# Mongo backup job (see app/backup/mongo_backup.py). The job itself no-ops at
# runtime unless BACKUP_S3_* is configured, but the binaries must exist in the
# image for it to ever work. Installed from MongoDB's official apt repo,
# matched to this base image's Debian codename (currently "bookworm").
# NOTE: not verified against an actual `docker build` in this environment (no
# Docker available here) — please confirm `mongodump --version` works on the
# first real build, and adjust the codename/repo line if the base image's
# Debian version has since changed.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl gnupg ca-certificates \
    && curl -fsSL https://pgp.mongodb.com/server-7.0.asc | gpg --dearmor -o /usr/share/keyrings/mongodb-server-7.0.gpg \
    && CODENAME=$(. /etc/os-release && echo "$VERSION_CODENAME") \
    && echo "deb [ signed-by=/usr/share/keyrings/mongodb-server-7.0.gpg ] https://repo.mongodb.org/apt/debian ${CODENAME}/mongodb-org/7.0 main" \
        > /etc/apt/sources.list.d/mongodb-org-7.0.list \
    && apt-get update && apt-get install -y --no-install-recommends mongodb-database-tools \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://localhost:'+os.getenv('PORT','8080')+'/health')" || exit 1

CMD ["python", "-m", "app.main"]
