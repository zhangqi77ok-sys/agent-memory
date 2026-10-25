import re

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_env: str = "dev"  # dev | test | prod
    env: str | None = None  # 支持 ENV=prod 环境变量
    service_keys: str = ""  # 逗号分隔的合法服务密钥，支持轮换
    service_key_users: str = ""  # 逗号分隔 key:user 对（第二十五轮）：绑定后该密钥即身份，自断言失效；为空 = 不绑定
    allow_unbound_assertion: bool = False  # 修复第 1 轮：显式声明单用户自用才允许未绑定密钥自断言身份；
    # 默认 False = 经中间件的远程断言一律 403（绑定密钥与进程内直调不受影响）
    admin_keys: str = ""  # 管理端密钥（逗号分隔，第十七轮）；为空 = 管理端接口关闭（404）
    database_url: str = "sqlite:///./dev.db"  # 服务运行账号（非 owner/非超级用户）
    worker_database_url: str | None = None  # Worker 账号 memsvc_worker（BYPASSRLS）；为空时用 database_url（仅 SQLite/开发）
    migrate_database_url: str | None = None  # 迁移账号（owner）；为空时用 database_url
    app_id: str = "tiancode"  # 固定应用标识，不由调用方切换
    embedding_model: str = "gemini-embedding-001"  # 向量供应商固定为 Google Gemini
    embedding_dim: int = 1536  # gemini-embedding-001 支持 768/1536/3072，与既有 pgvector 列维度一致
    embedding_provider: str = "gemini"  # 目前仅支持 gemini；留空表示关闭向量（召回走关键词）
    gemini_api_key: str = ""  # GEMINI_API_KEY；为空时不生成向量，写入/召回自动降级
    embedding_timeout_s: float = 30.0  # Worker 批量嵌入超时；召回侧改用 recall_timeout_s
    # OpenAI 兼容抽取接口。三项都空着时 Worker 启动失败，不会改用假抽取器。
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout_s: float = 30.0  # 抽取/提炼 LLM 调用超时；与 embedding_timeout_s 同量级
    recall_timeout_s: float = 2.0
    docs_enabled: bool | None = None  # Swagger UI；None=非 prod 开启、prod 关闭；演示部署可设 DOCS_ENABLED=true
    rate_limit_enabled: bool | None = None  # None: 非 test 环境开启，test 环境默认关闭
    rate_limit_read_per_min: int = 120  # 读接口限流阈值（次/分钟）
    rate_limit_write_per_min: int = 30  # 写/导出/删除接口限流阈值（次/分钟）
    memory_ttl_days: int = 0  # 记忆自动过期时长（天）；0 表示不过期
    episode_skeleton_days: int = 90  # 轨迹骨架化保留期（天，第十七轮）；0 表示不骨架化

    @property
    def is_rate_limit_enabled(self) -> bool:
        if self.rate_limit_enabled is not None:
            return self.rate_limit_enabled
        return self.effective_env != "test"

    @property
    def effective_env(self) -> str:
        return (self.env or self.app_env).strip().lower()

    @property
    def is_prod(self) -> bool:
        return self.effective_env == "prod"

    @property
    def service_key_list(self) -> list[str]:
        if not self.service_keys:
            return []
        return [k.strip() for k in self.service_keys.split(",") if k.strip()]

    @property
    def service_key_user_map(self) -> dict[str, str]:
        """SERVICE_KEY_USERS 解析：非法条目（无冒号、空 key/空用户）跳过；
        同一 key 出现多次时后者覆盖。只收窄不放宽——映射里的 key 仍须先过
        service_key_list 校验，中间件 401 在前，绑定本身授不了权。"""
        mapping: dict[str, str] = {}
        if not self.service_key_users:
            return mapping
        for entry in self.service_key_users.split(","):
            entry = entry.strip()
            if ":" not in entry:
                continue
            key, user = entry.split(":", 1)
            key, user = key.strip(), user.strip()
            if key and user:
                mapping[key] = user
        return mapping

    @property
    def admin_key_list(self) -> list[str]:
        if not self.admin_keys:
            return []
        return [k.strip() for k in self.admin_keys.split(",") if k.strip()]


settings = Settings()


def check_settings(s: Settings = settings) -> None:
    if not re.fullmatch(r"[a-z0-9_-]{1,64}", s.app_id or ""):
        raise RuntimeError("APP_ID 只能是 1-64 位小写字母、数字、- 或 _")
