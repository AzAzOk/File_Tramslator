# Build stage
FROM python:3.12-slim AS builder

RUN sed -i 's|http://|https://|g' /etc/apt/sources.list.d/debian.sources

COPY *.deb /tmp/debs/

RUN dpkg -i /tmp/debs/libssl1.1_*.deb || true && \
    dpkg -i /tmp/debs/openssl_*.deb || true && \
    dpkg -i /tmp/debs/ca-certificates_*.deb || true

WORKDIR /build

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install uv -i https://pypi.tuna.tsinghua.edu.cn/simple
RUN uv pip install --no-cache-dir --prefix=/install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# Production stage
FROM python:3.12-slim

RUN sed -i 's|http://|https://|g' /etc/apt/sources.list.d/debian.sources

COPY *.deb /tmp/debs/

RUN dpkg -i /tmp/debs/libssl1.1_*.deb || true && \
    dpkg -i /tmp/debs/openssl_*.deb || true && \
    dpkg -i /tmp/debs/ca-certificates_*.deb || true

# Install LibreOffice and Java (Debian Trixie ships OpenJDK 21)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-writer \
    openjdk-21-jre-headless \
    wget \
    unzip \
    && rm -rf /var/lib/apt/lists/*

# Download and install Okapi Tikal v1.48.0 (works with Java 8+)
RUN TIKAL_URL="https://okapiframework.org/binaries/main/1.48.0/okapi-apps_gtk2-linux-x86_64_1.48.0.zip" && \
    wget -q "$TIKAL_URL" -O /tmp/tikal.zip && \
    unzip -q /tmp/tikal.zip -d /opt && \
    rm /tmp/tikal.zip

# Find and set up tikal.sh (directory name may vary)
RUN find /opt -name "tikal.sh" 2>/dev/null | while read sh; do chmod +x "$sh"; done && \
    TIKAL_SH=$(find /opt -name "tikal.sh" 2>/dev/null | head -1) && \
    if [ -n "$TIKAL_SH" ]; then ln -sf "$TIKAL_SH" /usr/local/bin/tikal; fi

ENV TIKAL_HOME=/opt

WORKDIR /app

# Create non-root user for security
RUN groupadd -r appuser && useradd -r -g appuser appuser

# Copy installed dependencies from builder
COPY --from=builder /install /usr/local

# Copy application code
COPY file_translator/ ./file_translator/
COPY static/ ./static/

# Create necessary directories
RUN mkdir -p /app/logs /app/uploads && \
    chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

# Expose API port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# Start the application
CMD ["uvicorn", "file_translator.presentation.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
