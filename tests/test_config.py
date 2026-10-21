import pytest

from app.config import Settings, check_settings


def test_app_id_defaults_to_tiancode():
    assert Settings().app_id == "tiancode"


def test_prod_starts_without_any_jwt_secret():
    check_settings(Settings(app_env="prod"))  # 不再需要 JWT_SECRET


@pytest.mark.parametrize("bad", ["", "Tian Code", "租"])
def test_rejects_bad_app_id(bad):
    with pytest.raises(RuntimeError):
        check_settings(Settings(app_id=bad))


def test_jwt_settings_are_gone():
    assert not hasattr(Settings(), "jwt_secret")
