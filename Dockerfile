# ─── PGMRec Backend — Dockerfile ────────────────────────────────────────────
# Linux production image.  GPU encoders are NOT included here.
# To add GPU support later, extend this image with NVIDIA/CUDA base.
#
# Build:
#   docker build -t pgmrec-backend .
#
# Run (quick test):
#   docker run -p 8000:8000 -e PGMREC_ADMIN_PASSWORD=secret pgmrec-backend
#
# Production (with data volume):
#   docker run -d \
#     -p 8000:8000 \
#     -v /opt/pgmrec/data:/app/data \
#     -v /opt/pgmrec/logs:/app/logs \
#     --env-file /opt/pgmrec/.env \
#     --restart unless-stopped \
#     pgmrec-backend

FROM python:3.12-slim

# System dependencies: ffmpeg + ffprobe (required at runtime)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (layer cache)
COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy backend source
COPY backend/ /app/

# Optional: copy pre-built frontend so FastAPI can serve it as static files.
# Build with: cd frontend && npm run build
# The resulting dist/ folder will be served at / by FastAPI.
# If dist/ is absent, frontend must be served separately.
COPY frontend/dist/ /app/../frontend/dist/

# Data and log directories — override with volume mounts in production
RUN mkdir -p /app/data/channels /app/data/manifests /app/data/exports \
             /app/data/preview /app/logs/exports

# Expose API port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

# Non-root user for security
RUN useradd -r -u 1001 -g root pgmrec && \
    chown -R pgmrec /app
USER pgmrec

# Start uvicorn
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
