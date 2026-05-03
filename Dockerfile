# precog-hl runtime image — SMC v1.0
# Built in GitHub Actions → pushed to ghcr.io → pulled by Render
# Zero Render build minutes consumed per deploy.
FROM python:3.12-slim

# System deps: git for any runtime pulls, build tools for numpy wheels
RUN apt-get update && apt-get install -y --no-install-recommends     gcc     libc-dev     && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first (layer caching — only rebuilds if requirements change)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the app
COPY . .

# Persistent volume mount point (Render attaches disk at /var/data)
RUN mkdir -p /var/data
VOLUME ["/var/data"]

# SMC v1.0 entrypoint — Flask routes + WS + monitors via smc_app:app
# gunicorn config:
#   --workers 1: single worker (in-process state must be unified)
#   --threads 4: handle webhook + status concurrent requests
#   --timeout 120: allow long boot for HL WS subscription + initial cache fetch
CMD gunicorn smc_app:app --workers 1 --threads 4 --timeout 120 --bind 0.0.0.0:${PORT:-10000}
