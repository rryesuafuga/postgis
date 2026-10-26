# Client-side tooling for the demo: GDAL (ogr2ogr), PostGIS loaders (raster2pgsql),
# PostgreSQL 16 client (psql, pg_dump, pg_restore) and the Python stack.
# The postgis/postgis server image ships none of the loaders, hence this image.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PATH=/opt/venv/bin:$PATH \
    PYTHON=/opt/venv/bin/python

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      gdal-bin postgis postgresql-client-16 python3 python3-venv make curl ca-certificates libxml2-utils \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir -r /tmp/requirements.txt \
 && ln -s /opt/venv/bin/python /usr/local/bin/python

WORKDIR /work
CMD ["bash"]
