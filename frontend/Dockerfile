# =============================================================================
# Builds the control panel bundle. Nothing serves from this image -- Compose
# copies the output into a volume that Nginx serves as static files, so the
# panel costs nothing at runtime.
# =============================================================================
FROM node:22-alpine

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm install --no-audit --no-fund

COPY . .
RUN npm run build
