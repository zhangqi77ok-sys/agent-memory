import re

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_env: str = "dev"  # dev | test | prod
    database_url: str = "sqlite:///./dev.db"  # 服务运行账号（非 owner/非超级用户）
    worker_database_url: str | None = None  # Worker 账号 memsvc_worker（BYPASSRLS）；为空时用 database_url（仅 SQLite/开发）
    migrate_database_url: str | None = None  # 迁移账号（owner）；为空时用 database_url
    app_id: str = "tiancode"  # 固定应用标识，不由调用方切换
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536
    # OpenAI 兼容抽取接口。三项都空着时 Worker 启动失败，不会改用假抽取器。
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    recall_timeout_s: float = 2.0
    docs_enabled: bool | None = None  # Swagger UI；None=非 prod 开启、prod 关闭；演示部署可设 DOCS_ENABLED=true


settings = Settings()


def check_settings(s: Settings = settings) -> None:
    if not re.fullmatch(r"[a-z0-9_-]{1,64}", s.app_id or ""):
        raise RuntimeError("APP_ID 只能是 1-64 位小写字母、数字、- 或 _")
