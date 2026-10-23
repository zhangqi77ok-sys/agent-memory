"""一键版（lite，第十三轮）：单进程跑起整套服务，个人用户五分钟上手。

为什么需要它：标准形态是 API + Worker + Postgres + 迁移四件套，对「就想自己用」
的人太重。lite 把它们收进一个进程、一个 SQLite 文件：
  python -m app.lite
数据目录 ~/.agent-memory/（AGENT_MEMORY_HOME 可改）：memory.db + 自动生成并
持久化的 service_key.txt（服务密钥，本机自用）。启动时自动跑迁移到最新。

能力边界（诚实标注）：
- 不配 LLM（LLM_BASE_URL/LLM_API_KEY/LLM_MODEL）时：自动抽取/提炼关闭——
  写入会排队但不处理；关键词简报、Markdown 同步、Skills 导出、面板都可用。
  配上任意 OpenAI 兼容端点后 Worker 线程自动启用（抽取与提炼共用同一组配置）。
- 不配 GEMINI_API_KEY 时：向量召回关闭，关键词路径照常。
- SQLite 单机并发有限，这是个人版的取舍；要多用户/高并发请用标准形态（PG）。
"""
import os
import pathlib
import secrets
import threading


def prepare_env() -> tuple[pathlib.Path, str]:
    """在导入 app.* 之前把环境定好：数据目录、库地址、服务密钥。返回 (目录, 密钥)。"""
    home = pathlib.Path(os.environ.get("AGENT_MEMORY_HOME", "~/.agent-memory")).expanduser()
    home.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("DATABASE_URL", f"sqlite:///{home}/memory.db")
    os.environ.pop("WORKER_DATABASE_URL", None)  # 与 API 同库（个人版单机）
    os.environ.setdefault("APP_ENV", "prod")
    key = (os.environ.get("SERVICE_KEYS") or "").split(",")[0].strip()
    if not key:
        keyfile = home / "service_key.txt"
        if keyfile.exists():
            key = keyfile.read_text(encoding="utf-8").strip()
        else:
            key = secrets.token_urlsafe(24)
            keyfile.write_text(key, encoding="utf-8")
            keyfile.chmod(0o600)
        os.environ["SERVICE_KEYS"] = key
    return home, key


def llm_configured() -> bool:
    from app.config import settings

    return all([(settings.llm_base_url or "").strip(),
                (settings.llm_api_key or "").strip(),
                (settings.llm_model or "").strip()])


def main() -> None:  # pragma: no cover - 真入口，靠冒烟实跑验证
    home, key = prepare_env()
    from alembic import command
    from alembic.config import Config

    root = pathlib.Path(__file__).resolve().parents[1]
    command.upgrade(Config(str(root / "alembic.ini")), "head")

    import uvicorn
    from fastapi.staticfiles import StaticFiles

    from app.main import app

    if llm_configured():
        from app.worker import main as worker_main

        threading.Thread(target=worker_main, daemon=True, name="lite-worker").start()
        worker_note = "开（自动抽取/提炼）"
    else:
        worker_note = "关（未配 LLM_BASE_URL/LLM_API_KEY/LLM_MODEL，写入将排队）"
    # 前端是独立构建产物（web/dist）：在就挂上，不在就纯 API 并把构建办法说清。
    dist = root / "web" / "dist"
    if dist.is_dir():
        app.mount("/ui", StaticFiles(directory=str(dist), html=True), name="ui")
        ui_note = "面板 /ui/（星图 /ui/star-map.html）"
    else:
        ui_note = "未构建前端（纯 API）：先在 web/ 里执行 npm install && npm run build 再重启"
    port = int(os.environ.get("PORT", "8000"))
    print(f"agent-memory lite 已启动\n"
          f"  数据目录：{home}\n"
          f"  服务地址：http://127.0.0.1:{port}（{ui_note}）\n"
          f"  服务密钥：{key}（已存 {home}/service_key.txt，MCP/脚本用它连接）\n"
          f"  后台处理：{worker_note}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")


if __name__ == "__main__":
    main()
