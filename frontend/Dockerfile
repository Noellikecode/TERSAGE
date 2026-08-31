# syntax=docker/dockerfile:1
# Non-root, honours PORT, terminates on SIGTERM.
FROM node:20-slim AS deps
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --no-audit --no-fund

FROM node:20-slim AS builder
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
# Next inlines NEXT_PUBLIC_* into the client bundle at `next build`, so the
# browser key has to arrive here -- setting it on Cloud Run at runtime is too
# late and leaves the photorealistic view reporting UNCONFIGURED. Empty by
# default: a build without it behaves exactly as before, and the view says so.
# This key reaches every visitor's browser by design, so it must be a
# referrer-restricted browser key scoped to tile.googleapis.com -- never the
# server key the backend uses for the Solar API.
ARG NEXT_PUBLIC_GOOGLE_MAPS_API_KEY=""
ENV NEXT_PUBLIC_GOOGLE_MAPS_API_KEY=$NEXT_PUBLIC_GOOGLE_MAPS_API_KEY
COPY --from=deps /app/node_modules ./node_modules
COPY frontend/ ./
RUN npm run build

FROM node:20-slim AS runtime
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=8080 HOSTNAME=0.0.0.0
WORKDIR /app

RUN groupadd --system --gid 10001 firstdue \
 && useradd --system --uid 10001 --gid firstdue --create-home firstdue

COPY --from=builder --chown=firstdue:firstdue /app/.next/standalone ./
COPY --from=builder --chown=firstdue:firstdue /app/.next/static ./.next/static
COPY --from=builder --chown=firstdue:firstdue /app/public ./public

USER firstdue
EXPOSE 8080

# node receives SIGTERM as PID 1 and Next closes the server gracefully.
CMD ["node", "server.js"]
