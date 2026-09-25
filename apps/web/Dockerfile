# Web dashboard: a static Vite build served by Caddy with strict security headers.
# Build context is the repository root. Configuration comes in as build arguments (Railway passes
# service variables declared with ARG); none of it is secret, all of it ships to the browser.
FROM node:22-alpine AS build
WORKDIR /app
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY apps/web/ ./
ARG VITE_API_BASE
ARG VITE_AUTH_MODE=firebase
ARG VITE_FIREBASE_API_KEY
ARG VITE_FIREBASE_AUTH_DOMAIN
ARG VITE_FIREBASE_PROJECT_ID
ARG VITE_FIREBASE_APP_ID
# Local test sign-in is for a developer's machine only: never build it into an image.
RUN test -n "$VITE_API_BASE" || { echo "VITE_API_BASE is required" >&2; exit 1; }; \
    test "$VITE_AUTH_MODE" = "firebase" || { echo "images are always built with Firebase sign-in" >&2; exit 1; }; \
    npm run build

FROM caddy:2-alpine
COPY apps/web/Caddyfile /etc/caddy/Caddyfile
COPY --from=build /app/dist /srv
# No TLS or admin API here (Railway terminates TLS), so Caddy needs nothing writable but /tmp.
ENV XDG_DATA_HOME=/tmp/caddy/data XDG_CONFIG_HOME=/tmp/caddy/config
USER 65534:65534
EXPOSE 8080
CMD ["caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]
