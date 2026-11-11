# Conector de WhatsApp por QR Code (Baileys). Precisa de um volume em /data para a sessão.
FROM node:22-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
COPY package.json package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund
COPY index.js ./
ENV AUTH_DIR=/data
EXPOSE 3000
CMD ["node", "index.js"]
