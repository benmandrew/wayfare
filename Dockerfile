# Three stages, so no compiler ever reaches the shipped image.
#
#   tippecanoe  builds the felt fork from source -- no distro packages it, and
#               only that fork writes PMTiles
#   deps        compiles the Python dependencies, pycairo among them, into a venv
#   runtime     copies both results in and carries no build tooling at all
#
# The alternative -- installing build-essential in the runtime stage and purging
# it afterwards -- leaves the compiler in that layer's history whatever the purge
# does, and rebuilds it on every source change. Splitting the stages means editing
# wayfare/ rebuilds only the last few layers.

FROM debian:bookworm-slim AS tippecanoe
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential git libsqlite3-dev zlib1g-dev ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch 2.79.0 https://github.com/felt/tippecanoe.git /src \
    && make -C /src -j"$(nproc)" \
    && make -C /src install


FROM python:3.12-slim-bookworm AS deps
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libcairo2-dev pkg-config \
    && rm -rf /var/lib/apt/lists/*

# A venv rather than the system site-packages, because it is one self-contained
# directory to copy forward.
ENV VIRTUAL_ENV=/opt/venv PATH=/opt/venv/bin:$PATH
RUN python -m venv "$VIRTUAL_ENV"

WORKDIR /app
# Dependencies resolve from pyproject alone, so this layer survives every change
# to wayfare/ and only rebuilds when the dependency list moves. README stays out
# on purpose: pyproject carries no `readme` field, so copying it here would buy
# nothing and make every docs edit recompile pycairo.
COPY pyproject.toml ./
RUN mkdir -p wayfare && touch wayfare/__init__.py \
    && pip install --no-cache-dir '.[art]' \
    && pip uninstall -y wayfare


FROM python:3.12-slim-bookworm

# Runtime shared libraries only: tippecanoe's, and cairo's for the art renderer.
# curl is here for the compose healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libsqlite3-0 zlib1g libcairo2 curl \
    && rm -rf /var/lib/apt/lists/*


# The whole directory rather than a tippecanoe* glob. `make install` lays down
# six binaries and `tile-join` is the only one not carrying the prefix, so the
# glob dropped exactly one -- silently, since nothing in the build or the image
# looks for it. publish.py runs tippecanoe twice over different zoom ranges and
# uses tile-join to concatenate the passes into a single PMTiles file, so a
# national run spent its hours matching, exported the GeoJSONL, and only then
# died on a missing executable. This stage's base image ships an empty
# /usr/local/bin, so copying the directory carries tippecanoe's install output
# and nothing else.
COPY --from=tippecanoe /usr/local/bin/ /usr/local/bin/
COPY --from=deps /opt/venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv PATH=/opt/venv/bin:$PATH

WORKDIR /app
COPY pyproject.toml README.md ./
COPY wayfare ./wayfare
# Dependencies are already in the venv, so this installs the package itself and
# nothing else -- seconds, not minutes.
RUN pip install --no-cache-dir --no-deps .

# The viewer and the range-capable static server, so the `web` service serves
# straight out of this image.
COPY web ./web
COPY scripts ./scripts

# Not root, but the data volume has to be writable by whoever we become.
RUN useradd --uid 10001 --create-home wayfare && mkdir -p /data && chown wayfare /data
USER wayfare

ENV WAYFARE_DATA=/data
VOLUME /data

ENTRYPOINT ["wayfare"]
CMD ["status"]
