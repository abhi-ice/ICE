# syntax=docker/dockerfile:1
# ICE Site Ops backend (FastAPI) for Railway.
#
# Build from the REPO ROOT, not from backend/, so the Microsoft Project worker
# package (worker/) can be copied in for the admin "download worker" button:
#   docker build -f backend/Dockerfile .
# On Railway: leave Root Directory empty and set the service variable
# RAILWAY_DOCKERFILE_PATH=backend/Dockerfile.
#
# What goes into the build is controlled by backend/Dockerfile.dockerignore,
# which builders that support it use instead of the root .dockerignore. The
# root .dockerignore still leaves worker/ out on purpose, so an Emergent build
# behaves exactly as before.

# Python 3.12: numpy 2.4 and pandas 3.0 in requirements.txt need 3.11 or newer.
# Debian 12 (bookworm) is pinned so "default-jre-headless" is Java 17.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=UTC \
    JAVA_HOME=/opt/java

# System packages:
# - default-jre-headless: Java 17+ for the .mpp / .xer parser (MPXJ via JPype)
# - pango, harfbuzz, fontconfig, gdk-pixbuf, cairo, shared-mime-info: WeasyPrint PDFs
# - fonts-liberation, fonts-dejavu-core: fonts for PDF exports
# - poppler-utils: PDF page to image (pdf2image)
# - tesseract-ocr: drawing title-block text reading (pytesseract)
# - ca-certificates, curl: HTTPS calls out, and checking the api by hand
# The man1 folder must exist on slim images or the Java install fails.
# /opt/java is a link to whichever Java version Debian installs, so JAVA_HOME
# never has to name a version number; the build stops if libjvm.so is missing.
RUN mkdir -p /usr/share/man/man1 \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      default-jre-headless \
      libpango-1.0-0 \
      libpangoft2-1.0-0 \
      libharfbuzz0b \
      libharfbuzz-subset0 \
      libfontconfig1 \
      libgdk-pixbuf-2.0-0 \
      libcairo2 \
      shared-mime-info \
      fonts-liberation \
      fonts-dejavu-core \
      fonts-noto-cjk \
      poppler-utils \
      tesseract-ocr \
      ca-certificates \
      curl \
 && rm -rf /var/lib/apt/lists/* \
 && ln -s "$(dirname "$(dirname "$(readlink -f "$(command -v java)")")")" /opt/java \
 && test -e /opt/java/lib/server/libjvm.so \
 && java -version

# Install Python packages first, so a code change does not redo this slow step.
# Only the backend requirements are installed. Never install
# worker/requirements.txt here: it lists pywin32, which only exists on Windows
# (installing it broke the Linux build in the 2026-07-05 outage).
COPY backend/requirements.txt /app/backend/requirements.txt
# emergentintegrations is Emergent's private package: it stays in
# requirements.txt so Emergent keeps working, but is left out here.
# --no-deps installs exactly the pinned set: the file is a full freeze, and
# pip's resolver rejects one pin pair (httpx 0.27 vs google-genai) that
# works fine in practice. Do not add `pip check`.
RUN grep -viE '^emergentintegrations' /app/backend/requirements.txt > /tmp/requirements.railway.txt \
 && pip install --no-deps -r /tmp/requirements.railway.txt \
 && rm /tmp/requirements.railway.txt

# Run as a normal user, not root.
RUN useradd --create-home --uid 10001 app

# Application code.
COPY --chown=10001:10001 backend/ /app/backend/

# The Windows worker package, copied as plain files only (never installed).
# admin_settings_router.py zips /app/worker for the admin download button.
# The "worke[r]" pattern lets the build carry on without a worker folder if
# this builder fell back to the root .dockerignore (which leaves worker/ out);
# the download button then answers "not found" instead of the build failing.
COPY --chown=10001:10001 worke[r] /app/worker/

USER app
WORKDIR /app/backend

# Give the api a fixed PORT (8000) in its Railway variables, so the web service
# and the cron job can reach it at api.railway.internal:8000. "::" listens on
# both IPv4 and IPv6, which Railway's private network needs. One process only:
# the live-update list and the background loops assume a single process.
EXPOSE 8000
CMD ["python", "serve.py"]
