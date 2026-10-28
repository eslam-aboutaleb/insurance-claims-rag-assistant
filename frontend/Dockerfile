# ==============================================================================
# OmniCare Financial Frontend - Production Multi-Stage Dockerfile
# ==============================================================================
# Build stages:
#   1. deps     - Install Node.js dependencies (cached across builds).
#   2. builder  - Build the Vite production bundle.
#   3. runner   - Serve the built assets with a minimal Node.js runtime.
#
# Security considerations:
#   - Uses node:24-alpine (minimal attack surface).
#   - Runs as non-root user (appuser:UID 1001) in the final stage.
#   - Only copies built assets (dist/) and static files to the runner stage.
#   - Installs serve globally for static file serving (no build tools in prod).
# ==============================================================================

# ==============================================================================
# STAGE 1: Dependencies Installation
# ==============================================================================
# Install dependencies once and cache them across builds. This stage is only
# rebuilt when package.json or lock files change.
FROM node:24-alpine AS deps

# Install libc6-compat for Alpine Linux compatibility with native Node modules.
RUN apk add --no-cache libc6-compat

WORKDIR /app

# Copy lock files and package.json to leverage Docker layer caching.
# Supports multiple package managers: npm, yarn, pnpm, and bun.
COPY package.json package-lock.json* yarn.lock* pnpm-lock.yaml* bun.lockb* ./

# Install dependencies using the appropriate package manager.
# The conditional logic auto-detects the lock file and uses the correct command.
RUN \
  if [ -f yarn.lock ]; then yarn --frozen-lockfile; \
  elif [ -f package-lock.json ]; then npm ci; \
  elif [ -f pnpm-lock.yaml ]; then corepack enable pnpm && pnpm i --frozen-lockfile; \
  else npm install; \
  fi

# ==============================================================================
# STAGE 2: Vite Build
# ==============================================================================
# Build the production-optimized Vite bundle. Copies node_modules from the
# deps stage to avoid reinstalling dependencies.
FROM node:24-alpine AS builder

WORKDIR /app

# Copy installed dependencies from the deps stage.
COPY --from=deps /app/node_modules ./node_modules

# Copy the full source code for the build step.
COPY . .

# Build argument for the backend API URL.
# This is injected at build time and baked into the client bundle.
# In production, set this to the actual backend URL (e.g., https://api.omnicare.com).
ARG VITE_API_URL=http://localhost:8000
ENV VITE_API_URL=$VITE_API_URL

# Ensure the public directory exists for static asset copying.
RUN mkdir -p /app/public

# Build the Vite production bundle.
# The output is written to the dist/ directory.
RUN \
  if [ -f yarn.lock ]; then yarn build; \
  elif [ -f package-lock.json ]; then npm run build; \
  elif [ -f pnpm-lock.yaml ]; then corepack enable pnpm && pnpm run build; \
  else npm run build; \
  fi

# ==============================================================================
# STAGE 3: Production Runner
# ==============================================================================
# Minimal runtime image containing only the built assets and a static file
# server. No build tools, no source code, no node_modules.
FROM node:24-alpine AS runner

WORKDIR /app

# Set environment variables for the production server.
ENV NODE_ENV=production
ENV PORT=3000
ENV HOSTNAME="0.0.0.0"

# Create a non-root user and group for security.
# Running as non-root prevents container escape vulnerabilities.
RUN addgroup --system --gid 1001 nodejs
RUN adduser --system --uid 1001 appuser

# Copy only the built assets and static files from the builder stage.
COPY --from=builder /app/public ./public
COPY --from=builder /app/dist ./dist

# Install serve globally for static file serving.
# serve is a lightweight, production-ready static file server.
RUN npm install -g serve

# Switch to the non-root user for the final runtime.
USER appuser

# Expose the frontend server port.
EXPOSE 3000

# Serve the built Vite assets on port 3000.
# -s enables single-page application mode (all routes serve index.html).
CMD ["serve", "-s", "dist", "-l", "3000"]
