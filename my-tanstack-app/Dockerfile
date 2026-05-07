# TradingAgents webapp2 image (TanStack Start + Cloudflare).
#
# Build context: this directory. Build with:
#   docker build -t tradingagents-webapp2 .
#
# Or together with webapp1's backend:
#   docker compose -f my-tanstack-app/docker-compose.fullstack.yml up --build
#
# This file is fork-only and never present upstream — see ../FORK_PATCHES.md.
#
# We run Vite dev mode under the `@cloudflare/vite-plugin`, which spins up
# `workerd` locally so the `/api/*` server-route catch-all works exactly like
# it will on Cloudflare Workers in prod. workerd needs glibc, so this image is
# Debian slim, not Alpine.

FROM node:22-slim

ENV PNPM_HOME=/usr/local/share/pnpm \
    PATH=/usr/local/share/pnpm:$PATH \
    NODE_ENV=development

WORKDIR /app

# pnpm via Corepack — pinned to match the lockfile.
RUN corepack enable && corepack prepare pnpm@10.15.1 --activate

# Install dependencies in a separate layer so source changes don't bust the
# install cache.
COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

# Source.
COPY . .

# Entrypoint rewrites wrangler.jsonc with the runtime WEBAPP1_API_BASE so the
# in-network backend hostname reaches the worker, then starts Vite dev.
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 3000

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
