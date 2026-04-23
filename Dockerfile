# PRECOG v2 — Node.js execution server
# Pine indicator webhook → HL SDK execution
FROM node:20-slim

WORKDIR /app

# Install only prod deps
COPY package*.json ./
RUN npm install --omit=dev --no-audit --no-fund

# App code
COPY . .

# Persistent volume (not strictly used but keeps Render mount stable)
RUN mkdir -p /var/data
VOLUME ["/var/data"]

EXPOSE 3000
CMD ["node", "index.js"]
