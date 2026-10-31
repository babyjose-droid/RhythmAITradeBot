# Rhythm AI Trader dashboard — Next.js standalone, non-root.
FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
# Next.js resolves rewrites at build time, so the API address is a build argument.
ARG RHYTHM_API_URL=http://api:8000
ENV NEXT_TELEMETRY_DISABLED=1 RHYTHM_API_URL=${RHYTHM_API_URL}
RUN npm run build

FROM node:22-alpine AS runtime
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 PORT=3000 HOSTNAME=0.0.0.0
RUN addgroup -S -g 10001 rhythm && adduser -S -u 10001 -G rhythm rhythm
COPY --from=build --chown=rhythm:rhythm /app/.next/standalone ./
COPY --from=build --chown=rhythm:rhythm /app/.next/static ./.next/static
USER rhythm
EXPOSE 3000
CMD ["node", "server.js"]
