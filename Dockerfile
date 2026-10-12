# Multi-stage build for smaller final image
FROM python:3.11-slim AS builder
# Project label: lets the deploy scripts prune only this project's leftover
# images, never other images on the host.
LABEL org.opencontainers.image.title="shellysmartenergy"

# Install build dependencies
RUN echo 'APT::Sandbox::User "root";' > /etc/apt/apt.conf.d/00-no-sandbox
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Create and set the working directory
WORKDIR /app

# Copy requirements first for better Docker layer caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir whitenoise gunicorn

# Final stage - runtime image
FROM python:3.11-slim
# Project label: lets the deploy scripts prune only this project's leftover
# images, never other images on the host.
LABEL org.opencontainers.image.title="shellysmartenergy"

# Build arguments for version info
ARG VERSION=1.0.0
ARG BUILD_DATE=unknown

# Install only runtime dependencies (minimal)
RUN echo 'APT::Sandbox::User "root";' > /etc/apt/apt.conf.d/00-no-sandbox
RUN apt-get update && apt-get install -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV APP_VERSION=${VERSION}
ENV APP_BUILD_DATE=${BUILD_DATE}

# Create non-root user for security
RUN groupadd -r django && useradd -r -g django django

# Set the working directory
WORKDIR /app

# Copy Python packages from builder stage
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy project files (excluding files in .dockerignore)
COPY --chown=django:django . .

# Create build info file with version and build date
RUN echo "${VERSION}-${BUILD_DATE}" > /app/BUILD_INFO

# ==== Collect static files without needing the real DB ====
# Use SQLite in-memory DB so collectstatic doesn't touch /data/db.sqlite3 during build
ENV DJANGO_SQLITE_PATH=":memory:"

# Run migrations and collect static files
RUN python manage.py migrate && \
    python manage.py collectstatic --noinput && \
    # Clean up temporary files
    find . -type f -name "*.pyc" -delete && \
    find . -type d -name "__pycache__" -delete

# Reset for runtime - will use /data/db.sqlite3 if /data exists
ENV DJANGO_SQLITE_PATH=""

# Create data directory with proper permissions for SQLite database
RUN mkdir -p /data && \
    chown django:django /data && \
    chmod 755 /data

# Change ownership of the app directory to django user
RUN chown -R django:django /app

# Create an entrypoint script to handle database initialization
RUN echo '#!/bin/bash\n\
set -e\n\
\n\
echo "=== Django Container Startup ==="\n\
echo "User: $(whoami)"\n\
echo "UID: $(id -u)"\n\
\n\
# Fix /data directory permissions if running as root\n\
if [ "$(id -u)" = "0" ]; then\n\
    echo "Running as root, fixing permissions..."\n\
    mkdir -p /data\n\
    chown -R django:django /data\n\
    chmod -R 755 /data\n\
    echo "Permissions fixed. Note: You should run this container as user django for better security."\n\
fi\n\
\n\
# Ensure /data exists and has proper permissions\n\
mkdir -p /data\n\
\n\
echo "Data directory status:"\n\
ls -la /data/\n\
\n\
# Check if we can write to /data\n\
if ! touch /data/.test_write 2>/dev/null; then\n\
    echo "ERROR: Cannot write to /data directory!"\n\
    echo "Please ensure the container is run with proper volume permissions:"\n\
    echo "  docker run -v /host/path:/data:rw your-image"\n\
    echo "Or fix host directory permissions: chown 999:999 /host/path"\n\
    exit 1\n\
fi\n\
rm -f /data/.test_write\n\
\n\
# Initialize database if it doesnt exist\n\
if [ ! -f "/data/db.sqlite3" ]; then\n\
    echo "Initializing database at /data/db.sqlite3..."\n\
    python manage.py migrate\n\
    echo "Database initialized successfully."\n\
else\n\
    echo "Database exists at /data/db.sqlite3, running migrations..."\n\
    python manage.py migrate\n\
    echo "Migrations completed."\n\
fi\n\
\n\
# Ensure an admin exists. On a fresh DB this creates one with a RANDOM one-time\n\
# password printed below; if admin already exists nothing changes. Safe to run\n\
# every start because there is no hardcoded default password.\n\
python create_test_user.py\n\
\n\
echo "Final database status:"\n\
ls -la /data/db.sqlite3 || echo "Database file not found!"\n\
\n\
echo "Starting Gunicorn server..."\n\
# Start the application\n\
exec "$@"' > /app/entrypoint.sh && \
chmod +x /app/entrypoint.sh

# Switch to non-root user
USER django

# Expose port 8000
EXPOSE 8000

# Set the entrypoint
ENTRYPOINT ["/app/entrypoint.sh"]

# Start Django server with Gunicorn
CMD ["gunicorn", "--bind=0.0.0.0:8000", "--workers=3", "--timeout=300", "--max-requests=1000", "--max-requests-jitter=50", "project.wsgi:application"]
