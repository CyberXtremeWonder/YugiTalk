FROM python:3.12.14-slim-trixie@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f AS dependencies
WORKDIR /build
COPY requirements.runtime.txt .
# 构建环境安装锁定依赖，运行镜像只接收隔离虚拟环境。
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -r requirements.runtime.txt

FROM python:3.12.14-slim-trixie@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f
LABEL io.yugitalk.project="yugitalk" io.yugitalk.component="backend"
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUTF8=1 PATH="/opt/venv/bin:$PATH"
WORKDIR /app
COPY --from=dependencies /opt/venv /opt/venv
# 基础镜像仍需应用发行版安全更新；实际安装版本由镜像扫描报告记录。
RUN apt-get update && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* && useradd --create-home yugitalk
COPY app ./app
COPY start.py alembic.ini ./
COPY migrations ./migrations
USER yugitalk
EXPOSE 8000
CMD ["python", "start.py", "--host", "0.0.0.0"]
