# ODA File Converter service — DWG ↔ DXF conversion via ODAFileConverter
#
# Prerequisites:
#   Place ODAFileConverter_QT6_lnxX64_*.deb in the project root directory
#   (next to this Dockerfile's build context).
#
# Usage:
#   docker build -t oda-converter -f oda-converter/Dockerfile .
#   docker run --rm -v shared_data:/data oda-converter

FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV DISPLAY=:99

# Switch apt to HTTPS (environment blocks HTTP due to proxy auth)
RUN sed -i 's|http://|https://|g' /etc/apt/sources.list

# Pre-install SSL/cert debs so HTTPS works in apt
COPY *.deb /tmp/debs/
RUN dpkg -i /tmp/debs/libssl1.1_*.deb || true && \
    dpkg -i /tmp/debs/openssl_*.deb || true && \
    dpkg -i /tmp/debs/ca-certificates_*.deb || true

# System deps: xvfb for headless GUI, libs for ODAFileConverter
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb \
    libxcb-xinerama0 \
    libxcb-util1 \
    libxcb-image0 \
    libxcb-keysyms1 \
    libxcb-render-util0 \
    libxcb-shape0 \
    libxcb-xfixes0 \
    libxcb-cursor0 \
    libxcb-icccm4 \
    libxcb-xkb1 \
    libxkbcommon-x11-0 \
    libxkbcommon0 \
    libegl1 \
    libgl1 \
    libfontconfig1 \
    libsm6 \
    libice6 \
    wget \
    ca-certificates \
    python3 \
    && rm -rf /var/lib/apt/lists/*

# Install ODAFileConverter from the bundled .deb
COPY ODAFileConverter_QT6_lnxX64_*.deb /tmp/odafc.deb
RUN dpkg -i /tmp/odafc.deb || true && \
    apt-get install -f -y && \
    rm /tmp/odafc.deb

# Ensure the binary is findable
RUN ln -sf /usr/bin/ODAFileConverter /usr/local/bin/odafc 2>/dev/null || true

# Copy the converter service wrapper
COPY oda-converter/server.py /app/server.py
COPY oda-converter/converter.py /app/converter.py

WORKDIR /app

# Health check
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:5000/health')" || exit 1

EXPOSE 5000

# Start Xvfb as a persistent background process, then run the server directly
CMD ["bash", "-c", "chmod 777 /data && Xvfb :99 -screen 0 1280x1024x24 -nolisten tcp & sleep 2 && exec python3 /app/server.py"]