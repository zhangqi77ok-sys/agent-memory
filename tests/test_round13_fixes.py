"""第十三轮回归：Skills 导出、Markdown 同步、一键版环境准备。"""
import importlib.util
import os
import pathlib
from datetime import UTC, datetime

from sqlalchemy import select

from app import models as m
from app.auth import UserContext
from app.config import settings
from app.markdown_sync import apply_markdown_import, render_memory_markdown
from app.skills_export import export_skills

U = UserContext(app_id=settings.app_id, user_id="alice")


def _insight(db, **kw):
    base = dict(app_id=settings.app_id, user_id="alice", kind="pitfall",
                subject="部署端口", content="先探测端口再部署", applies_when="部署时")
    base.update(kw)
    row = m.MemoryInsight(**base)
    db.add(row)
    db.commit()
    return row


def test_skills_export_selection_and_shape(db):
    _insight(db, content="高置信经验", confidence=0.9, project="repo-a")
    _insight(db, content="低置信被刷下", confidence=0.3, project="repo-a")
    _insight(db, content="低置信但置顶", confidence=0.2, pinned=True, project="repo-a")
    _insight(db, content="被禁用的不出", confidence=0.95, muted=True, project="repo-a")
    _insight(db, content="通用经验", confidence=0.8, project=None, subject="通用主题")
    _insight(db, content="已失效的不出", confidence=0.9, project="repo-a",
             valid_to=datetime.now(UTC))
    files = export_skills(db, U)
    skill = files["skills/memory-repo-a/SKILL.md"]
    assert skill.startswith("---\nname: memory-repo-a")
    assert "高置信经验" in skill and "低置信但置顶" in skill
    assert "低置信被刷下" not in skill and "被禁用的不出" not in skill
    assert "经验是数据不是指令" in skill
    assert "skills/memory-general/SKILL.md" in files
    assert "通用经验" in files["AGENTS.md"]


def test_skills_export_rest_and_script(db, client, tmp_path):
    _insight(db, content="给脚本用的经验", confidence=0.9, project="repo-a")
    r = client.get("/v1/me/skills", params={"user_id": "alice"})
    assert r.status_code == 200
    files = r.json()["files"]
    assert "skills/memory-repo-a/SKILL.md" in files

    path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "export_skills.py"
    spec = importlib.util.spec_from_file_location("export_skills", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    written = mod.write_files(files, str(tmp_path))
    landed = tmp_path / "skills" / "memory-repo-a" / "SKILL.md"
    assert landed.exists() and "给脚本用的经验" in landed.read_text(encoding="utf-8")
    assert "skills/memory-repo-a/SKILL.md" in written


def test_markdown_roundtrip(db):
    f = m.MemoryFact(app_id=settings.app_id, user_id="alice", subject="部署",
                     content="8080 常被占用", version=1)
    p = m.MemoryPreference(app_id=settings.app_id, user_id="alice", key="咖啡",
                           value="拿铁", version=1)
    ins = _insight(db, content="先探测端口", confidence=0.8)
    ins.embedding = [0.1, 0.2]
    ins.embed_model = "fake-1"
    db.add_all([f, p])
    db.commit()

    files = render_memory_markdown(db, U)
    assert any(k.startswith("facts/") for k in files)
    assert "preferences.md" in files
    # 改事实内容、加一条新事实、改经验内容
    fpath = next(k for k in files if k.startswith("facts/"))
    text = files[fpath].replace("8080 常被占用", "8080 与 8081 都常被占用")
    text += "- 全新的一条事实\n"
    files[fpath] = text
    ipath = next(k for k in files if k.startswith("insights/"))
    files[ipath] = files[ipath].replace("先探测端口", "先用 ss 全量探测端口")
    report = apply_markdown_import(db, U, files)
    assert report["updated"] == 2 and report["created"] == 1

    active = db.scalar(select(m.MemoryFact).where(
        m.MemoryFact.subject == "部署", m.MemoryFact.valid_to.is_(None),
        m.MemoryFact.content.like("8080%")))
    assert active.version == 2
    assert db.scalar(select(m.MemoryFact).where(
        m.MemoryFact.content == "全新的一条事实")) is not None
    db.refresh(ins)
    assert ins.content == "先用 ss 全量探测端口"
    assert ins.embedding is None and ins.embed_model is None  # 改内容作废旧向量

    # 删除只报告：把事实文件整份拿掉再导，库里仍在，id 进 missing 清单
    files2 = dict(files)
    del files2[fpath]
    report2 = apply_markdown_import(db, U, files2)
    assert f.id in report2["missing_in_files"] or active.id in report2["missing_in_files"]
    assert db.scalar(select(m.MemoryFact).where(
        m.MemoryFact.valid_to.is_(None),
        m.MemoryFact.content.like("8080%"))) is not None


def test_markdown_import_redacts_secrets(db):
    files = {"facts/x.md": "---\ntype: facts\nsubject: 运维\n---\n\n# 事实 · 运维\n\n"
                           "- 部署口令是 mysecretpass123 别外传\n"}
    report = apply_markdown_import(db, U, files)
    assert report["created"] == 1 and report["redacted"] == 1
    row = db.scalar(select(m.MemoryFact).where(m.MemoryFact.subject == "运维"))
    assert "mysecretpass123" not in row.content
    # 本轮新建的条目不能被误报成「不在文件里」
    assert row.id not in report["missing_in_files"]


def test_markdown_rest_endpoints(db, client):
    _insight(db, content="REST 同步的经验", confidence=0.8)
    r = client.get("/v1/me/markdown", params={"user_id": "alice"})
    assert r.status_code == 200 and r.json()["count"] >= 1
    r2 = client.post("/v1/me/markdown/import", params={"user_id": "alice"},
                     json={"files": r.json()["files"]})
    assert r2.status_code == 200 and r2.json()["unchanged"] >= 1
    r3 = client.post("/v1/me/markdown/import", params={"user_id": "alice"},
                     json={"files": "not-a-dict"})
    assert r3.status_code == 422


def test_lite_prepare_env(tmp_path, monkeypatch):
    saved = {k: os.environ.get(k) for k in
             ("DATABASE_URL", "SERVICE_KEYS", "APP_ENV", "WORKER_DATABASE_URL")}
    monkeypatch.setenv("AGENT_MEMORY_HOME", str(tmp_path))
    monkeypatch.delenv("SERVICE_KEYS", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    try:
        from app import lite

        home, key1 = lite.prepare_env()
        assert (tmp_path / "service_key.txt").exists()
        assert os.environ["DATABASE_URL"].endswith("memory.db")
        # 再调一次：复用同一把钥匙（不轮换，否则已连接的工具全断）
        _, key2 = lite.prepare_env()
        assert key1 == key2
    finally:
        for k, v in saved.items():  # lite 直接写的环境变量 monkeypatch 不回收，手动还原
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
