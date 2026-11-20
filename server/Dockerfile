FROM node:22-alpine

WORKDIR /app

COPY server/package*.json ./
RUN npm ci --omit=dev

COPY server/index.js ./index.js

ENV NODE_ENV=production
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=5 \
  CMD node -e "fetch('http://localhost:5000/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

USER node
CMD ["node", "index.js"]
