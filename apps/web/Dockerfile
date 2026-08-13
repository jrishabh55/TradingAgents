# Drishti web — PRODUCTION image (TanStack Start, Node server target).
#
# Multi-stage: `WEB_TARGET=node vite build` compiles the app (SSR bundle +
# hashed client assets), then a slim runtime stage serves it with plain Node
# via server-node.mjs. No Vite, no workerd, no HMR socket in production —
# the old dev-mode image hot-patched every open tab on redeploy, corrupting
# the running page's module graph (see server-node.mjs).
#
# Vite inlines VITE_*-prefixed vars into the client bundle AT BUILD TIME, so
# the Clerk publishable key must arrive as a build arg (compose `build.args`),
# not runtime environment. WEBAPP1_API_BASE stays runtime — the /api proxy
# route reads it per request.
#
# Dev images (vite dev + HMR + workerd) live in Dockerfile.dev.
# This file is fork-only and never present upstream — see ../FORK_PATCHES.md.

FROM node:22-slim AS build

ENV PNPM_HOME=/usr/local/share/pnpm \
    PATH=/usr/local/share/pnpm:$PATH

WORKDIR /app

RUN corepack enable && corepack prepare pnpm@10.15.1 --activate

COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

COPY . .

# Baked into the client bundle — passing it as ARG (not .env, which
# .dockerignore excludes) keeps dev keys out of prod images.
ARG VITE_CLERK_PUBLISHABLE_KEY
ENV VITE_CLERK_PUBLISHABLE_KEY=$VITE_CLERK_PUBLISHABLE_KEY

RUN WEB_TARGET=node pnpm build

# Guard against client/SSR CSS hash drift: the SSR bundle references the
# styles-<hash>.css name it computed for src/styles.css; if the Tailwind
# output differed between the two Vite environments, that name was never
# emitted and every page 404s its stylesheet. Alias the real client CSS to
# the SSR-expected name — a no-op when the hashes agree (the normal case,
# with tailwind's source() pinned in styles.css).
RUN ssr=$(grep -rho 'styles-[A-Za-z0-9_-]*\.css' dist/server/ | sort -u | head -1); \
    real=$(ls dist/client/assets/ | grep -o 'styles-.*\.css' | head -1); \
    echo "ssr-referenced: $ssr | client-emitted: $real"; \
    if [ -n "$ssr" ] && [ -n "$real" ] && [ "$ssr" != "$real" ]; then \
      cp "dist/client/assets/$real" "dist/client/assets/$ssr"; \
      echo "aliased $real -> $ssr"; \
    fi


FROM node:22-slim

ENV NODE_ENV=production \
    PNPM_HOME=/usr/local/share/pnpm \
    PATH=/usr/local/share/pnpm:$PATH

WORKDIR /app

RUN corepack enable && corepack prepare pnpm@10.15.1 --activate

# Runtime deps only (react, router, start server, srvx, …) — the dev
# toolchain (vite, wrangler, workerd, typescript) stays in the build stage.
COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile --prod

COPY --from=build /app/dist ./dist
COPY server-node.mjs ./

EXPOSE 3000

CMD ["node", "server-node.mjs"]
