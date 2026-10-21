import pathlib
import subprocess

DOCS = ["docs/api.md", "docs/architecture.md", "docs/lessons/me-ui.md", "README.md", "web/README.md"]
ALLOWED = ("migrations/versions/0001_init.py", "migrations/versions/0003_user_setting.py",
           "migrations/versions/0005_app_id.py", "migrations/versions/0006_drop_tenant_id.py",
           "migrations/versions/0007_memory_job_stats_fn.py",
           "docs/superpowers/", "tests/test_docs_wording.py", "tests/test_postgres.py", "tests/test_app_id.py")


def test_public_docs_have_no_jwt_login():
    for p in DOCS:
        t = pathlib.Path(p).read_text(encoding="utf-8")
        assert "JWT" not in t and "?token=" not in t, p


def test_no_tenant_wording_outside_history():
    out = subprocess.run(["git", "grep", "-il", "-e", "tenant", "-e", "租户"], capture_output=True, text=True).stdout
    bad = [p for p in out.split() if not p.startswith(ALLOWED)]
    assert bad == [], bad
