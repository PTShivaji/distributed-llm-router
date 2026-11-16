# syntax=docker/dockerfile:1
# Production image for the React frontend.
#   docker build -f frontend/Dockerfile -t llm-router-web:<git-sha> .        (build context = repository root)
# Stage 1 installs exact dependencies (npm ci) and refuses to produce an image unless typecheck, lint,
# the full test suite, the production build and the bundle secret check all pass.
# Stage 2 serves only the static dist/ with an unprivileged Caddy (SPA fallback, cache headers).
# No backend secret or provider key exists in this build: credentials are entered at runtime in the UI.

ARG NODE_IMAGE=node:22.23.4-alpine
ARG CADDY_IMAGE=caddy:2.11.9-alpine

# ---------------------------------------------------------------- build + verify
FROM ${NODE_IMAGE} AS build
WORKDIR /app/frontend
ENV CI=true \
    NPM_CONFIG_UPDATE_NOTIFIER=false \
    NPM_CONFIG_FUND=false \
    NPM_CONFIG_AUDIT=false \
    NPM_CONFIG_FETCH_RETRIES=5 \
    NPM_CONFIG_FETCH_RETRY_MINTIMEOUT=20000 \
    NPM_CONFIG_FETCH_RETRY_MAXTIMEOUT=120000 \
    # Build stage only: Docker bridge networks often have no IPv6 route; prefer IPv4 so registry
    # downloads do not stall on unreachable IPv6 addresses (observed: npm ci idle timeout).
    NODE_OPTIONS=--dns-result-order=ipv4first
COPY frontend/package.json frontend/package-lock.json ./
# Exact versions from package-lock.json; the npm download cache is a BuildKit cache mount (not in the image).
RUN --mount=type=cache,target=/root/.npm npm ci
COPY frontend/ ./
# Same-origin deployment: the edge proxy serves the app and forwards /api, /health and /metrics.
ENV VITE_API_BASE_URL=""
RUN npm run typecheck
RUN npm run lint
RUN npm run test
RUN npm run build
RUN npm run check:bundle

# ---------------------------------------------------------------- static server
FROM ${CADDY_IMAGE} AS runtime
LABEL org.opencontainers.image.title="llm-router-web" \
      org.opencontainers.image.description="Distributed Multi-Model Router frontend (static SPA)"
RUN addgroup -S -g 10002 web && adduser -S -D -H -u 10002 -G web -s /sbin/nologin web
COPY infrastructure/web.Caddyfile /etc/caddy/Caddyfile
COPY --from=build /app/frontend/dist /srv
# Caddy keeps nothing important here (admin, autosave and HTTPS are off); point its state at the /tmp tmpfs
# so the root filesystem can be read-only.
ENV XDG_CONFIG_HOME=/tmp/caddy-config \
    XDG_DATA_HOME=/tmp/caddy-data
USER 10002:10002
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
  CMD ["wget", "-q", "-O", "/dev/null", "http://127.0.0.1:8080/"]
CMD ["caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]
