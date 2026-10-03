# Build context is the repo root (npm workspaces keep one lockfile there).
FROM node:20-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
COPY demo/package.json demo/
COPY extension/package.json extension/
RUN npm ci -w demo --include-workspace-root=false --no-audit --no-fund
COPY demo/tsconfig.json demo/
COPY demo/src demo/src
RUN npx tsc -p demo

FROM node:20-alpine
WORKDIR /app
ENV NODE_ENV=production
COPY package.json package-lock.json ./
COPY demo/package.json demo/
COPY extension/package.json extension/
RUN npm ci -w demo --omit=dev --include-workspace-root=false --no-audit --no-fund
COPY --from=build /app/demo/dist demo/dist
WORKDIR /app/demo
# Entry point chosen per service by compose: node -r ./dist/tracing.js dist/<service>.js
