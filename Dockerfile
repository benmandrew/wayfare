# Runtime image for the pipeline stages. tippecanoe is built from source because
# no distro packages the felt fork, and only that fork writes PMTiles.

FROM debian:bookworm-slim AS tippecanoe
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential git libsqlite3-dev zlib1g-dev ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch 2.79.0 https://github.com/felt/tippecanoe.git /src \
    && make -C /src -j"$(nproc)" \
    && make -C /src install


FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
        libsqlite3-0 zlib1g curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=tippecanoe /usr/local/bin/tippecanoe* /usr/local/bin/

WORKDIR /app
COPY pyproject.toml README.md ./
COPY wayfare ./wayfare
RUN pip install --no-cache-dir .

# Not root, but the data volume has to be writable by whoever we become.
RUN useradd --uid 10001 --create-home wayfare && mkdir -p /data && chown wayfare /data
USER wayfare

ENV WAYFARE_DATA=/data
VOLUME /data

ENTRYPOINT ["wayfare"]
CMD ["status"]
