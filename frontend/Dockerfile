# syntax=docker/dockerfile:1
# ICE Site Ops website for the Railway "web" service.
# Build context: the frontend/ folder (Railway Root Directory = /frontend).
#
# Step 1 builds the React site with yarn. Step 2 serves it with Caddy, which
# also forwards /api (and the /api/ws WebSocket) to the api service.

# ---- Step 1: build the site ---------------------------------------------
FROM node:22-bookworm-slim AS build
WORKDIR /app

# The full site address, for example https://app.example.com. It is baked into
# the JavaScript at build time. Railway passes service variables to the build
# only when they are named here with ARG.
ARG REACT_APP_BACKEND_URL

# Install packages with yarn exactly as locked in yarn.lock. package-lock.json
# also exists in this folder, but it is not used. The node image already has
# yarn 1.22, which matches the "packageManager" line in package.json.
COPY package.json yarn.lock ./
RUN yarn --version \
 && yarn install --frozen-lockfile --non-interactive --network-timeout 600000 \
 && yarn cache clean

COPY . .

# Stop the build early with a clear message if the site address is missing;
# without it, links and the live-update connection point at the wrong place.
# CI=false keeps lint warnings as warnings; no source maps in the public site.
RUN test -n "$REACT_APP_BACKEND_URL" \
      || { echo "REACT_APP_BACKEND_URL must be set on the web service (for example https://app.example.com)"; exit 1; } \
 && REACT_APP_BACKEND_URL="$REACT_APP_BACKEND_URL" CI=false GENERATE_SOURCEMAP=false yarn build

# ---- Step 2: serve it with Caddy ----------------------------------------
FROM caddy:2-alpine

COPY Caddyfile /etc/caddy/Caddyfile
COPY --from=build /app/build /srv

# Check the Caddyfile while building, so a typo fails the build, not the deploy.
RUN caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile

# Railway sets PORT; the Caddyfile listens on it.
CMD ["caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]
