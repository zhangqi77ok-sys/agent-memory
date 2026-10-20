from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "sqlite:///./dev.db"
    jwt_secret: str = "dev-only-change-me-32-bytes-minimum!"
    jwt_alg: str = "HS256"
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536
    recall_timeout_s: float = 2.0


settings = Settings()
