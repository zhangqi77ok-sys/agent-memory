from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_env: str = "dev"  # dev | test | prod
    database_url: str = "sqlite:///./dev.db"  # 服务运行账号（非 owner/非超级用户）
    worker_database_url: str | None = None  # Worker 账号 memsvc_worker（BYPASSRLS）；为空时用 database_url（仅 SQLite/开发）
    migrate_database_url: str | None = None  # 迁移账号（owner）；为空时用 database_url
    jwt_secret: str = "dev-only-change-me-32-bytes-minimum!"
    jwt_alg: str = "HS256"
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536
    recall_timeout_s: float = 2.0


settings = Settings()


def check_settings(s: Settings = settings) -> None:
    if s.app_env not in ("dev", "test") and s.jwt_secret.startswith("dev-only-"):
        raise RuntimeError("JWT_SECRET 仍是公开仓库里的默认值，非开发环境拒绝启动")
