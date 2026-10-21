import pathlib

DOCS = ["docs/api.md", "docs/architecture.md", "docs/lessons/me-ui.md", "README.md", "web/README.md"]


def test_public_docs_have_no_jwt_login():
    for p in DOCS:
        t = pathlib.Path(p).read_text(encoding="utf-8")
        assert "JWT" not in t and "?token=" not in t, p
