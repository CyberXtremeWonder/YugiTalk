# 复用项目已构建的后端依赖，仅用于无外网联调，不用于部署。
FROM yugitalk-backend:0.1.0
USER root
RUN apt-get update && apt-get install -y --no-install-recommends aria2 \
    && rm -rf /var/lib/apt/lists/*
