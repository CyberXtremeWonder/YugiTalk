FROM node:22-bookworm-slim@sha256:c3de60bf2f9dd0ac6370e6117950ff62d6e339527e7472301c9c78a017978392 AS build
WORKDIR /app
COPY package.json package-lock.json ./
# 安装后清理下载缓存，避免构建镜像占用 Docker 所在磁盘。
RUN npm ci --include=optional --cache /tmp/yugitalk-npm-cache \
    && node -e "require('@rolldown/binding-linux-x64-gnu'); require('lightningcss')" \
    && rm -rf /tmp/yugitalk-npm-cache
COPY . .
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.30-alpine@sha256:15c994d10d6d78658721c3bcafff14cb281fba2a4bdf9d5ba92c416a472516e3
LABEL io.yugitalk.project="yugitalk" io.yugitalk.component="frontend"
USER root
# 应用发行版安全更新，运行时继续使用非 root 用户。
RUN apk upgrade --no-cache
USER 101
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 8080
