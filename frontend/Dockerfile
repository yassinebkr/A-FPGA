# Frontend: build with node, serve static with nginx.
# The SPA talks to the backend at <hostname>:8000 directly (see
# src/hooks/useAgentSocket.js), so no API proxy is needed here.
FROM node:22 AS build
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci || npm install
COPY . .
# Backend port the SPA connects to (compose passes 8001; dev default is 8000).
ARG VITE_BACKEND_PORT=8000
ENV VITE_BACKEND_PORT=$VITE_BACKEND_PORT
RUN npm run build

FROM nginx:alpine
# SPA fallback: unknown paths serve index.html (client-side routing).
RUN printf 'server {\n  listen 80;\n  root /usr/share/nginx/html;\n  location / { try_files $uri $uri/ /index.html; }\n}\n' \
  > /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
