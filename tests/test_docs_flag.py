from app.config import Settings


def _docs(s: Settings) -> bool:
    return s.docs_enabled if s.docs_enabled is not None else s.app_env != "prod"


def test_docs_off_in_prod_by_default():
    assert _docs(Settings(app_env="prod", jwt_secret="x" * 32)) is False


def test_docs_flag_overrides_prod():
    assert _docs(Settings(app_env="prod", jwt_secret="x" * 32, docs_enabled=True)) is True
