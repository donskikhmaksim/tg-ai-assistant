FROM python:3.12-slim

WORKDIR /app

# mongodb-database-tools (mongodump / mongorestore) for the OPTIONAL scheduled
# Mongo backup job (see app/backup/mongo_backup.py). The job itself no-ops at
# runtime unless BACKUP_S3_* is configured, but the binaries must exist in the
# image for it to ever work.
#
# Installed via direct download of MongoDB's prebuilt .deb (NOT via MongoDB's
# apt repo): the `python:3.12-slim` base image now resolves to Debian trixie,
# whose apt/sqv verifier rejects MongoDB's repo signing key (SHA1
# self-signature, considered insecure since 2026-02-01) with
# "Sub-process /usr/bin/sqv returned an error code (1)" — an external
# MongoDB/Debian compatibility issue with no fixed timeline. MongoDB does not
# yet publish a trixie build, so we grab the debian12 (bookworm) .deb; its
# only deps (libc6, libgssapi-krb5-2, libkrb5-3, libk5crypto3, libcomerr2,
# libkrb5support0, libkeyutils1) are ordinary shared libs present in trixie's
# repos, and bookworm-linked binaries run fine against trixie's newer glibc.
# `apt-get install ./*.deb` (rather than `dpkg -i`) resolves those deps
# automatically. Bump MONGO_TOOLS_VERSION when a newer release is needed;
# check https://www.mongodb.com/try/download/database-tools for the current
# stable 100.x version.
ARG MONGO_TOOLS_VERSION=100.17.0
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates \
    && curl -fsSL -o /tmp/mongodb-database-tools.deb \
        "https://fastdl.mongodb.org/tools/db/mongodb-database-tools-debian12-x86_64-${MONGO_TOOLS_VERSION}.deb" \
    && apt-get install -y --no-install-recommends /tmp/mongodb-database-tools.deb \
    && rm -f /tmp/mongodb-database-tools.deb \
    && rm -rf /var/lib/apt/lists/* \
    && mongodump --version

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request; urllib.request.urlopen('http://localhost:'+os.getenv('PORT','8080')+'/health')" || exit 1

CMD ["python", "-m", "app.main"]
