FROM node:24-bookworm-slim@sha256:d6aa754f16b3197301076f047b5def2f02ea1dbbc2ca920407d46d7ec7f87b20 AS node
FROM debian:13.1-slim@sha256:a347fd7510ee31a84387619a492ad6c8eb0af2f2682b916ff3e643eb076f925a
ARG DEBIAN_SNAPSHOT
# Link against Debian EGL/GLES libraries; use the box's Mali implementation at runtime.
RUN rm -f /etc/apt/sources.list.d/* && \
    printf 'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/%s/ trixie main\n' "$DEBIAN_SNAPSHOT" > /etc/apt/sources.list && \
    arm64=; cross=; \
    if [ "$(dpkg --print-architecture)" != arm64 ]; then \
        dpkg --add-architecture arm64; arm64=:arm64; cross="gcc-aarch64-linux-gnu libc6-dev-arm64-cross"; \
    fi && \
    apt-get update && apt-get install -y --no-install-recommends \
        $cross gcc libc6-dev ca-certificates curl git pkgconf python3 rsync rustup shellcheck xz-utils \
        libegl-dev$arm64 libgles-dev$arm64 && \
    rm -rf /var/lib/apt/lists/*
COPY --from=node /usr/local/ /usr/local/
ENV LANG=C.UTF-8
WORKDIR /work
