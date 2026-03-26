FROM node:20-alpine AS builder

WORKDIR /app
COPY package*.json ./

RUN npm ci --omit=dev

COPY . .

FROM node:20-alpine
RUN addgroup -S appgroup && adduser -S appuser -G appgroup

WORKDIR /app

COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/server.js    ./server.js
COPY --from=builder /app/public       ./public

USER appuser

EXPOSE 3000

CMD ["node", "server.js"]
