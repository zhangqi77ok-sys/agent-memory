import pytest

from app.config import Settings, check_settings


def test_refuses_default_jwt_secret_outside_dev():
    with pytest.raises(RuntimeError):
        check_settings(Settings(app_env="prod"))
    check_settings(Settings(app_env="prod", jwt_secret="x" * 40))
    check_settings(Settings(app_env="dev"))
