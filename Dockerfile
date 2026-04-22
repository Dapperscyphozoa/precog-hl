# precog-hl runtime image
# Built in GitHub Actions → pushed to ghcr.io → pulled by Render
# Zero Render build minutes consumed per deploy.
FROM python:3.12-slim

# System deps: git for any runtime pulls, build tools for numpy wheels
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libc-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first (layer caching — only rebuilds if requirements change)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy the app
COPY . .

# Persistent volume mount point (Render attaches disk at /var/data)
RUN mkdir -p /var/data
VOLUME ["/var/data"]

# Default command — overridden by Render per-service:
#   worker:  python3 precog.py
#   web:     python3 precog.py   (same — Flask is embedded)
CMD ["python3", "precog.py"]
