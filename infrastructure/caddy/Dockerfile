# syntax=docker/dockerfile:1
# Edge proxy image: official Caddy + the rate_limit handler (github.com/mholt/caddy-ratelimit),
# which is not part of the standard Caddy build. Versions are pinned (plugin pinned to a commit).
#   docker build -f infrastructure/caddy/Dockerfile -t llm-router-edge:<git-sha> .   (context = repository root)

ARG CADDY_VERSION=2.11.9
ARG RATELIMIT_COMMIT=5625512f24f6

FROM caddy:${CADDY_VERSION}-builder-alpine AS build
ARG CADDY_VERSION
ARG RATELIMIT_COMMIT
RUN xcaddy build v${CADDY_VERSION} --with github.com/mholt/caddy-ratelimit@${RATELIMIT_COMMIT}

FROM caddy:${CADDY_VERSION}-alpine AS runtime
LABEL org.opencontainers.image.title="llm-router-edge" \
      org.opencontainers.image.description="TLS-terminating reverse proxy for the Distributed Multi-Model Router"
COPY --from=build /usr/bin/caddy /usr/bin/caddy
COPY infrastructure/Caddyfile /etc/caddy/Caddyfile
# Unprivileged user. Ports 80/443 are bound via the per-container sysctl
# net.ipv4.ip_unprivileged_port_start=0 (set in docker-compose.prod.yml), so no capability is needed.
# xcaddy sets the file capability cap_net_bind_service on the binary; with cap_drop: ALL and
# no-new-privileges the kernel refuses to exec such a file ("operation not permitted"), so strip it.
RUN setcap -r /usr/bin/caddy \
 && [ -z "$(getcap /usr/bin/caddy)" ] \
 && addgroup -S -g 10003 edge && adduser -S -D -H -u 10003 -G edge -s /sbin/nologin edge \
 && mkdir -p /data /config \
 && chown -R 10003:10003 /data /config \
 # Validation provisions the internal CA; keep that key material out of the image layers.
 && XDG_DATA_HOME=/tmp/validate XDG_CONFIG_HOME=/tmp/validate \
    caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile \
 && rm -rf /tmp/validate \
 && [ -z "$(find /data /config ! -type d)" ]
# /data holds certificates and the ACME account: mount a persistent named volume there.
ENV XDG_DATA_HOME=/data \
    XDG_CONFIG_HOME=/config
USER 10003:10003
EXPOSE 80 443
CMD ["caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]
