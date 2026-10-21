from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def _docs(s: Settings) -> bool:
    return s.docs_enabled if s.docs_enabled is not None else not s.is_prod


def test_docs_off_in_prod_by_default():
    assert _docs(Settings(app_env="prod")) is False
    assert _docs(Settings(env="prod")) is False


def test_docs_flag_overrides_prod():
    assert _docs(Settings(app_env="prod", docs_enabled=True)) is True
    assert _docs(Settings(env="prod", docs_enabled=True)) is True


def test_prod_docs_endpoints_return_404():
    # 无论是 APP_ENV=prod 还是 ENV=prod，默认访问 /docs 和 /openapi.json 均返回 404
    prod_app1 = create_app(Settings(app_env="prod", docs_enabled=None))
    client1 = TestClient(prod_app1)
    assert client1.get("/docs").status_code == 404
    assert client1.get("/openapi.json").status_code == 404

    prod_app2 = create_app(Settings(env="prod", docs_enabled=None))
    client2 = TestClient(prod_app2)
    assert client2.get("/docs").status_code == 404
    assert client2.get("/openapi.json").status_code == 404


def test_docs_endpoints_return_200_when_enabled():
    # 非 prod 环境或显式 DOCS_ENABLED=True 时，文档正常开启
    dev_app = create_app(Settings(app_env="dev", docs_enabled=None))
    client_dev = TestClient(dev_app)
    assert client_dev.get("/docs").status_code == 200
    assert client_dev.get("/openapi.json").status_code == 200

    prod_app_enabled = create_app(Settings(app_env="prod", docs_enabled=True))
    client_enabled = TestClient(prod_app_enabled)
    assert client_enabled.get("/docs").status_code == 200
    assert client_enabled.get("/openapi.json").status_code == 200
