# 多阶段：构建静态资源 → nginx 托管 /memory/ui/
FROM node:20-alpine AS build
WORKDIR /src
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
# 生产默认走同域 /memory → 外层 Nginx 转发到 api
ARG VITE_API_BASE=/memory
ENV VITE_API_BASE=$VITE_API_BASE
RUN npm run build

FROM nginx:1.27-alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
# Vite base=/memory/ui/ 时产物在 dist/ 根下，资源路径已带 /memory/ui/ 前缀；
# 需要把文件放到 html/memory/ui/ 才能被 location 命中
COPY --from=build /src/dist /usr/share/nginx/html/memory/ui
EXPOSE 80
HEALTHCHECK --interval=30s --timeout=3s CMD wget -qO- http://127.0.0.1/healthz | grep -q ok || exit 1
