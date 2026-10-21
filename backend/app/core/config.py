"""集中校验环境配置，生产环境拒绝不完整或不安全的默认设置。"""

from pathlib import Path
from typing import Literal, Self, cast
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    """环境变量覆盖根目录 .env；CLI 只覆盖显式允许的启动参数。"""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )
    app_env: Literal["development", "test", "production"] = "development"
    app_host: str = "127.0.0.1"
    app_port: int = Field(default=8000, ge=1, le=65535)
    app_reload: bool = False
    # 本机默认地址与开发 Compose 的宿主端口一致；容器内地址由环境变量覆盖。
    database_url: SecretStr = SecretStr(
        "postgresql://yugitalk:dev-database-only@127.0.0.1:25432/yugitalk"
    )
    redis_url: SecretStr = SecretStr("redis://127.0.0.1:26379/0")
    storage_endpoint: str = "http://127.0.0.1:29000"
    storage_access_key: SecretStr = SecretStr("dev-storage-user")
    storage_secret_key: SecretStr = SecretStr("dev-storage-password")
    storage_bucket: str = "yugitalk"
    storage_region: str = "us-east-1"
    storage_ca_file: str = ""
    storage_addressing_style: Literal["path", "virtual"] = "path"
    # 仅隔离验收覆盖文件启用；正式外部S3必须使用HTTPS。
    storage_local_test_fixture: bool = False
    milvus_uri: str = "http://127.0.0.1:29530"
    milvus_token: SecretStr = SecretStr("")
    milvus_database: str = "default"
    model_provider: Literal["mock", "openai_compatible", "anthropic"] = "mock"
    model_id: str = "mock-v1"
    model_api_key: SecretStr = SecretStr("")
    model_base_url: str = ""
    model_ca_file: str = ""
    # 自定义工具仅调用运维明确允许的公共域名；凭证值只留在服务端配置中。
    tool_http_allowed_hosts: list[str] = Field(default_factory=list)
    tool_secrets: dict[str, SecretStr] = Field(default_factory=dict)
    amap_api_key: SecretStr = SecretStr("")
    tavily_api_key: SecretStr = SecretStr("")
    # 嵌入模型独立于聊天模型；默认调用本地离线 BGE-M3 服务。
    embedding_provider: Literal["openai_compatible", "mock"] = "openai_compatible"
    embedding_model: str = "BAAI/bge-m3"
    embedding_revision: str = "5617a9f61b028005a4858fdac845db406aefb181"
    embedding_dimension: int = Field(default=1024, ge=64, le=4096)
    embedding_base_url: str = "http://127.0.0.1:18081/v1"
    embedding_api_key: SecretStr = SecretStr("dev-embedding-key")
    embedding_timeout_seconds: float = Field(default=120, ge=1, le=300)
    vector_collection_prefix: str = Field(default="yugitalk_kb", pattern=r"^[a-z][a-z0-9_]{0,38}$")
    retrieval_min_score: float = Field(default=0.35, ge=0, le=1)
    # 七步 RAG 的次数、并发和输入边界由程序约束，模型不得自行放宽。
    rag_timeout_seconds: float = Field(default=60, ge=1, le=180)
    rag_parallel_queries: int = Field(default=4, ge=1, le=4)
    rag_fast_model: str = ""
    rag_fast_api_key: SecretStr = SecretStr("")
    rag_graph_enabled: bool = True
    rag_grader_model: str = ""
    rag_grader_api_key: SecretStr = SecretStr("")
    rag_candidate_limit: int = Field(default=24, ge=5, le=48)
    rag_context_characters: int = Field(default=5000, ge=700, le=8000)
    rag_merge_ratio: float = Field(default=0.5, gt=0, le=1)
    rag_hitl_ttl_seconds: int = Field(default=1800, ge=60, le=7200)
    rag_rerank_url: str = ""
    rag_rerank_model: str = ""
    rag_rerank_api_key: SecretStr = SecretStr("")
    rag_rerank_timeout_seconds: float = Field(default=3, gt=0, le=15)
    rag_rerank_max_characters: int = Field(default=12000, ge=700, le=24000)
    rag_rerank_failure_threshold: int = Field(default=3, ge=1, le=10)
    rag_rerank_cooldown_seconds: float = Field(default=30, ge=1, le=300)
    session_secret: SecretStr = SecretStr("dev-session-key-not-for-production")
    session_ttl_seconds: int = Field(default=28_800, ge=60, le=604_800)
    registration_enabled: bool = True
    smtp_host: str = ""
    smtp_port: int = Field(default=465, ge=1, le=65535)
    smtp_tls: Literal["ssl", "starttls"] = "ssl"
    smtp_username: str = ""
    smtp_password: SecretStr = SecretStr("")
    smtp_sender: str = ""
    auth_trusted_origins: list[str] = [
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:8080",
        "http://localhost:8080",
        "http://127.0.0.1:8000",
        "http://127.0.0.1:18000",
    ]
    cors_origins: list[str] = []
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    dependency_timeout_seconds: float = Field(default=3, gt=0, le=30)
    task_timeout_seconds: int = Field(default=300, ge=1)
    # 文件索引可能包含数百个分块，不复用普通对话的执行时限。
    document_index_timeout_seconds: int = Field(default=1800, ge=60, le=7200)
    task_queue: str = Field(default="yugitalk", pattern=r"^[a-zA-Z0-9_-]{1,80}$")
    run_lease_seconds: int = Field(default=45, ge=10, le=300)
    recovery_interval_seconds: int = Field(default=5, ge=1, le=60)
    run_max_attempts: int = Field(default=5, ge=1, le=10)
    max_model_steps: int = Field(default=12, ge=1, le=100)
    # 保留旧环境变量兼容，执行层不再按工具次数限额。
    max_tool_calls: int = Field(default=20, ge=1, le=200)
    # 默认不按 Token 额度拦截对话；用量统计、超时与并发边界继续生效。
    cumulative_budget_enabled: bool = False
    task_budget_enabled: bool = False
    workspace_token_budget: int = Field(default=100_000, ge=1)
    user_token_budget: int = Field(default=100_000, ge=1)
    workspace_concurrency: int = Field(default=4, ge=1, le=32)
    user_concurrency: int = Field(default=2, ge=1, le=16)
    model_timeout_seconds: float = Field(default=30, ge=1, le=120)
    model_max_retries: int = Field(default=2, ge=0, le=3)
    # 流式协议包含逐帧元数据与非正文增量，容量独立于回答正文和 Token 额度。
    model_max_stream_bytes: int = Field(default=4_194_304, ge=262_144, le=16_777_216)
    # 文字在生成层按 128 字符／150ms 合并；此处限制持久事件数，不限制 Token 数。
    # 上限仍为终态预留位置，异常不能让事件流悄悄结束。
    run_event_limit: int = Field(default=1000, ge=20, le=5000)
    run_event_retention_seconds: int = Field(default=86_400, ge=60)
    otel_exporter_otlp_endpoint: str = ""
    otel_sample_ratio: float = Field(default=0.1, ge=0, le=1)
    metrics_token: SecretStr = SecretStr("")

    @model_validator(mode="after")
    def validate_environment(self) -> Self:
        """先检查 URL 格式，再校验生产边界；错误消息不包含原始值。"""
        db = urlsplit(self.database_url.get_secret_value())
        redis = urlsplit(self.redis_url.get_secret_value())
        storage = urlsplit(self.storage_endpoint)
        milvus = urlsplit(self.milvus_uri)
        if db.scheme not in {"postgresql", "postgres"} or not db.hostname or not db.path[1:]:
            raise ValueError("DATABASE_URL 必须是带数据库名的 PostgreSQL 地址")
        if redis.scheme not in {"redis", "rediss"} or not redis.hostname:
            raise ValueError("REDIS_URL 必须是有效的 Redis 地址")
        if storage.scheme not in {"http", "https"} or not storage.hostname:
            raise ValueError("STORAGE_ENDPOINT 必须是有效的 HTTP 或 HTTPS 地址")
        if storage.username or storage.password or storage.query or storage.fragment:
            raise ValueError("STORAGE_ENDPOINT不允许嵌入凭证或查询参数")
        if self.storage_ca_file and not Path(self.storage_ca_file).is_file():
            raise ValueError("STORAGE_CA_FILE必须是已挂载的CA文件")
        if milvus.scheme not in {"http", "https"} or not milvus.hostname:
            raise ValueError("MILVUS_URI 必须是有效的 HTTP 或 HTTPS 地址")
        if self.embedding_provider != "mock":
            endpoint = urlsplit(self.embedding_base_url)
            if (
                endpoint.scheme not in {"http", "https"}
                or not endpoint.hostname
                or endpoint.username
                or endpoint.query
                or endpoint.fragment
            ):
                raise ValueError("EMBEDDING_BASE_URL 必须是有效的嵌入服务前缀")
        if self.app_env == "production":
            # 开发默认值只用于本机启动；生产额外要求真实服务、独立凭证与明确的 HTTPS 来源。
            if len(self.metrics_token.get_secret_value()) < 32:
                raise ValueError("生产 METRICS_TOKEN 必须使用至少 32 位的独立随机值")
            if self.embedding_provider == "mock":
                raise ValueError("生产环境必须配置真实嵌入模型")
            if (
                not self.embedding_api_key.get_secret_value()
                or self.embedding_api_key.get_secret_value().startswith("dev-")
            ):
                raise ValueError("生产嵌入服务必须配置独立认证密钥")
            if not self.auth_trusted_origins or any(
                urlsplit(value).scheme != "https"
                or not urlsplit(value).hostname
                or urlsplit(value).path not in {"", "/"}
                or bool(
                    urlsplit(value).username or urlsplit(value).query or urlsplit(value).fragment
                )
                for value in self.auth_trusted_origins
            ):
                raise ValueError("生产登录来源必须显式配置 HTTPS 白名单")
            if not self.milvus_token.get_secret_value():
                raise ValueError("生产 Milvus 必须显式配置认证令牌")
            if self.app_reload or self.log_level == "DEBUG":
                raise ValueError("生产环境禁止热重载与 DEBUG 日志")
            if self.model_provider == "mock" or not self.model_api_key.get_secret_value():
                raise ValueError("生产环境必须显式配置真实模型与模型密钥")
            if self.model_id == "mock-v1":
                raise ValueError("生产环境必须显式配置 MODEL_ID")
            endpoint = urlsplit(self.model_base_url)
            if (
                endpoint.scheme != "https"
                or not endpoint.hostname
                or endpoint.username
                or endpoint.query
                or endpoint.fragment
            ):
                raise ValueError("生产模型服务必须使用无嵌入凭证的 HTTPS 地址")
            if len(
                self.session_secret.get_secret_value()
            ) < 32 or self.session_secret.get_secret_value().startswith("dev-"):
                raise ValueError("生产 SESSION_SECRET 必须使用至少 32 位的独立随机值")
            secrets = [self.storage_access_key, self.storage_secret_key]
            if any(
                not value.get_secret_value() or value.get_secret_value().startswith("dev-")
                for value in secrets
            ):
                raise ValueError("生产存储凭证必须显式配置")
            if not db.password or db.password.startswith("dev-"):
                raise ValueError("生产数据库密码必须显式配置")
            if not redis.password:
                raise ValueError("生产 Redis 必须配置认证")
            local_storage_fixture = (
                # 隔离验收的 HTTP 存储例外同时绑定固定服务地址和入口，不能作为通用跳过开关。
                self.storage_local_test_fixture
                and self.storage_endpoint == "http://storage:9000"
                and self.auth_trusted_origins == ["https://localhost:18443"]
            )
            if self.storage_local_test_fixture and not local_storage_fixture:
                raise ValueError("本机S3测试例外仅允许固定隔离端点与localhost入口")
            if storage.scheme != "https" and not local_storage_fixture:
                raise ValueError("正式S3服务必须使用HTTPS")
            if "*" in self.cors_origins or any(
                not value.startswith("https://") for value in self.cors_origins
            ):
                raise ValueError("生产 CORS 仅允许明确的 HTTPS 来源，或同源空列表")
        return self


def load_settings(environment: str | None = None) -> Settings:
    """CLI 显式环境优先；未指定时遵循环境变量与配置文件。"""
    if environment is None:
        return Settings()
    if environment not in {"development", "test", "production"}:
        raise ValueError("运行环境无效")
    return Settings(app_env=cast(Literal["development", "test", "production"], environment))
