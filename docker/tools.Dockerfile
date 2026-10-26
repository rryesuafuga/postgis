# Client-side tooling for the demo: GDAL (ogr2ogr), PostGIS loaders (raster2pgsql),
# PostgreSQL 16 client (psql, pg_dump, pg_restore) and the Python stack.
# The postgis/postgis server image ships none of the loaders, hence this image.
# Also the workspace image of the dev container (.devcontainer/, GitHub Codespaces).
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PATH=/opt/venv/bin:$PATH \
    PYTHON=/opt/venv/bin/python

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      gdal-bin postgis postgresql-client-16 python3 python3-venv make curl git ca-certificates libxml2-utils \
 && rm -rf /var/lib/apt/lists/*

# Optional extra CA certificates, e.g. for an office proxy that inspects TLS: put *.crt files in
# docker/certs/ before building. pip and the downloaders then trust the system bundle.
COPY docker/certs/ /usr/local/share/ca-certificates/extra/
RUN update-ca-certificates
ENV PIP_CERT=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

COPY requirements.txt /tmp/requirements.txt
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt \
 && ln -s /opt/venv/bin/python /usr/local/bin/python

WORKDIR /work
CMD ["bash"]
