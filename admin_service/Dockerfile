# Admin service image.
# Build context is the repository root so the shared `file_translator` package
# (roles/grants stores, Mongo repositories, config seeder) can be imported.
FROM python:3.12-slim

RUN sed -i 's|http://|https://|g' /etc/apt/sources.list.d/debian.sources

# The build context is the repository root, so the packages are addressed from
# it: Docker rejects a `..` in a COPY source, which is why this cannot be
# `../*.deb` the way it looks from inside admin_service/.
COPY *.deb /tmp/debs/

RUN dpkg -i /tmp/debs/libssl1.1_*.deb || true && \
    dpkg -i /tmp/debs/openssl_*.deb || true && \
    dpkg -i /tmp/debs/ca-certificates_*.deb || true

WORKDIR /app

COPY admin_service/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Shared domain/infrastructure code + the admin service itself.
COPY file_translator/ ./file_translator/
COPY admin_service/ ./admin_service/
# The administrator's guide, rendered on demand by GET /api/docs/admin-guide.
# Only this one file is needed - the service never reads the rest of docs/.
COPY docs/admin-guide.md ./docs/admin-guide.md

RUN groupadd -r appuser && useradd -r -g appuser appuser && \
    chown -R appuser:appuser /app

USER appuser

# Container port. docker-compose publishes it to the host on 127.0.0.1:8011.
EXPOSE 8011

HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8011/api/health')" || exit 1

CMD ["uvicorn", "admin_service.app:app", "--host", "0.0.0.0", "--port", "8011"]
