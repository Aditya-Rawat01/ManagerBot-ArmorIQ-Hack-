# ProxyLead web development image (P1-11, ADR-029).
#
# LOCAL DEVELOPMENT ONLY. The deployed web app is built and hosted by Vercel
# (ADR-030); this image exists so `docker compose up` gives teammates a
# working Vite dev server without a local Node install.
#
# Build from the repository root (the build context):
#   docker build -f deploy/web/Dockerfile -t proxylead-web:dev .
#
# Runs `npm run dev -- --host 0.0.0.0` (port 5173 comes from
# apps/web/vite.config.ts, `strictPort: true`). `VITE_API_BASE_URL` is read
# by Vite at dev-server startup from the container environment — never bake
# secrets into VITE_* variables: everything VITE_* ends up in the public
# browser bundle (see apps/web/.env.example).

FROM node:22-slim

WORKDIR /app/apps/web

# Install dependencies first for layer caching. .dockerignore keeps the
# host's node_modules out of the context, so this is always a clean install
# from the lockfile.
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci

# Application source. At runtime compose bind-mounts ./apps/web over this
# directory (with an anonymous volume preserving node_modules), so host
# edits reach the dev server without a rebuild.
COPY apps/web ./

# Run as the image's non-root `node` user (ADR-029).
RUN chown -R node:node /app
USER node

EXPOSE 5173

CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
