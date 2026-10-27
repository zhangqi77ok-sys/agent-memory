"""第 40 轮：多模态入库（截图 / 语音复盘以文本层进记忆）。

设计见 round40-design-draft.md。诚实边界：服务端不解析像素、不转录
音频；文本层一律由调用方提供，origin 只做标注。媒体备忘 = 标准
episode（source=screenshot/voice-memo）+ 一行 sidecar 引用元数据。
"""

import hashlib

from sqlalchemy import select

from app import models as m
from app.media import (
    build_media_steps,
    chunk_text_layer,
    submit_media_batch,
    validate_media,
)
from scripts import media_cli
from tests.conftest import consent, uid

SCREENSHOT_BODY = {
    "user_id": "alice", "kind": "screenshot", "origin": "ocr",
    "title": "登录接口 500 报错截图",
    "text": "Traceback: NullPointer at login.py:42\n500 Internal Server Error",
    "outcome": "failure",
}
VOICE_BODY = {
    "user_id": "alice", "kind": "voice", "origin": "transcript",
    "title": "周复盘：登录接口翻车",
    "text": "这周登录接口挂了两次，都是空指针。以后上线前先跑登录冒烟。",
}


def test_screenshot_text_layer_lands_as_episode_with_sidecar(client, db):
    consent(client, "alice")
    r = client.post("/v1/media", json=SCREENSHOT_BODY)
    assert r.status_code == 202, r.text
    ep_id = r.json()["episode_id"]
    assert ep_id
    ep = db.get(m.TaskEpisode, ep_id)
    assert ep is not None and ep.source == "screenshot"
    assert ep.outcome == "failure"
    assert any("NullPointer" in (s.get("observation") or "") for s in ep.steps)
    ref = db.scalar(select(m.MemoryMediaRef).where(
        m.MemoryMediaRef.episode_id == ep_id))
    assert ref is not None and ref.kind == "screenshot" and ref.origin == "ocr"


def test_voice_transcript_lands_and_lists(client, db):
    consent(client, "alice")
    r = client.post("/v1/media", json=VOICE_BODY)
    assert r.status_code == 202, r.text
    ep_id = r.json()["episode_id"]
    r = client.get("/v1/media", params=uid("alice"))
    assert r.status_code == 200, r.text
    items = r.json()
    assert any(it["episode_id"] == ep_id and it["kind"] == "voice" for it in items)
    r = client.get(f"/v1/media/{ep_id}", params=uid("alice"))
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "周复盘：登录接口翻车"


def test_media_get_other_user_404(client):
    consent(client, "alice", "bob")
    r = client.post("/v1/media", json=SCREENSHOT_BODY)
    ep_id = r.json()["episode_id"]
    r = client.get(f"/v1/media/{ep_id}", params=uid("bob"))
    assert r.status_code == 404, r.text


def test_switch_off_skips_media(client):
    r = client.post("/v1/media", json=SCREENSHOT_BODY)
    assert r.status_code == 202, r.text
    assert r.json()["skipped"] is True
    assert r.json()["episode_id"] is None


def test_unsupported_mime_rejected_explicitly(client):
    consent(client, "alice")
    body = {**SCREENSHOT_BODY, "mime": "application/pdf"}
    r = client.post("/v1/media", json=body)
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["code"] == "unsupported_media_type"


def test_empty_text_layer_rejected(client):
    consent(client, "alice")
    body = {**SCREENSHOT_BODY, "text": "   "}
    r = client.post("/v1/media", json=body)
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["code"] == "empty_text_layer"


def test_tombstoned_media_blocked_409(client, db):
    from app.config import settings
    from app.tombstone import record_episode_tombstone

    consent(client, "alice")
    # 墓碑按媒体备忘实际入库的步骤形态记（action 前缀 + 文本块）
    steps = build_media_steps("screenshot", SCREENSHOT_BODY["text"])
    record_episode_tombstone(db, settings.app_id, "alice",
                             SCREENSHOT_BODY["title"], steps)
    db.commit()
    r = client.post("/v1/media", json=SCREENSHOT_BODY)
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "tombstoned"


def test_batch_partial_success(client, db):
    consent(client, "alice")
    r = client.post("/v1/media/batch", json={
        "user_id": "alice",
        "items": [SCREENSHOT_BODY, VOICE_BODY,
                  {**SCREENSHOT_BODY, "mime": "application/pdf"}]})
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    assert len(results) == 3
    assert results[0]["ok"] is True and results[0]["episode_id"]
    assert results[1]["ok"] is True and results[1]["episode_id"]
    assert results[2]["ok"] is False
    assert results[2]["error"] == "unsupported_media_type"
    # 好条已入库，坏条没拖垮整批
    assert db.get(m.TaskEpisode, results[0]["episode_id"]) is not None


def test_idempotency_key_no_duplicate_sidecar(client, db):
    consent(client, "alice")
    h = {"Idempotency-Key": "bw-media-test-1"}
    r1 = client.post("/v1/media", json=SCREENSHOT_BODY, headers=h)
    r2 = client.post("/v1/media", json=SCREENSHOT_BODY, headers=h)
    assert r1.status_code == 202 and r2.status_code == 202, (r1.text, r2.text)
    assert r1.json()["episode_id"] == r2.json()["episode_id"]
    refs = db.scalars(select(m.MemoryMediaRef).where(
        m.MemoryMediaRef.episode_id == r1.json()["episode_id"])).all()
    assert len(refs) == 1


def test_chunking_splits_long_lines_and_caps_steps():
    long_text = "x" * 4500 + "\n" + "y" * 100
    chunks = chunk_text_layer(long_text)
    assert len(chunks) == 4  # 4500 → 2000+2000+500，另加 100
    assert all(len(c) <= 2000 for c in chunks)
    many = "\n".join(f"第{i}行" for i in range(300))
    chunks = chunk_text_layer(many)
    assert len(chunks) == 200
    assert chunks[-1].endswith("（文本层超长，已截断）")


def test_secret_in_text_layer_redacted(client, db):
    consent(client, "alice")
    body = {**VOICE_BODY,
            "text": "复盘：这次用的 key 是 sk-abcdefghij1234567890，别外传"}
    r = client.post("/v1/media", json=body)
    assert r.status_code == 202, r.text
    ep = db.get(m.TaskEpisode, r.json()["episode_id"])
    blob = " ".join((s.get("observation") or "") for s in ep.steps)
    assert "sk-abcdefghij1234567890" not in blob
    assert "[密钥]" in blob


def test_clear_all_deletes_sidecar(client, db):
    from app.deletion import delete_user_memory_data

    consent(client, "alice")
    r = client.post("/v1/media", json=SCREENSHOT_BODY)
    ep_id = r.json()["episode_id"]
    assert db.scalar(select(m.MemoryMediaRef).where(
        m.MemoryMediaRef.episode_id == ep_id)) is not None
    delete_user_memory_data(db, "tiancode", "alice")
    db.commit()
    assert db.scalar(select(m.MemoryMediaRef).where(
        m.MemoryMediaRef.episode_id == ep_id)) is None


def test_media_cli_strips_srt_timestamps(tmp_path):
    srt = ("1\n00:00:01,000 --> 00:00:03,000\n"
           "登录接口又挂了\n\n2\n00:00:04,000 --> 00:00:06,000\n"
           "还是空指针\n")
    p = tmp_path / "memo.srt"
    p.write_text(srt, encoding="utf-8")
    text = media_cli.read_text_layer(str(p))
    assert "00:00:01" not in text and "登录接口又挂了" in text
    assert "还是空指针" in text


def test_media_cli_original_meta(tmp_path):
    f = tmp_path / "err.png"
    data = b"\x89PNG fake bytes"
    f.write_bytes(data)
    meta = media_cli.original_meta(str(f))
    assert meta["byte_size"] == len(data)
    assert meta["sha256"] == hashlib.sha256(data).hexdigest()
    assert meta["mime"] == "image/png"
    assert meta["filename"] == "err.png"


def test_media_service_batch_direct(db):
    # 服务层不查同意门（REST/MCP 入口查）；这里直接用 db 会话测批量隔离
    from app.auth import UserContext

    u = UserContext(app_id="tiancode", user_id="carol")
    results = submit_media_batch(db, u, [
        {"kind": "voice", "origin": "note", "title": "口头复盘",
         "text": "今天把缓存键写错了"},
        {"kind": "screenshot", "origin": "ocr", "title": "坏的",
         "text": "x", "mime": "application/pdf"},
    ])
    assert results[0]["ok"] is True
    assert results[1]["ok"] is False
    assert results[1]["error"] == "unsupported_media_type"


def test_validate_media_rejects_bad_kind_and_oversize():
    for kwargs, code in [
        ({"kind": "video", "origin": "note", "text": "x", "mime": None, "byte_size": None},
         "invalid_kind"),
        ({"kind": "voice", "origin": "note", "text": "x", "mime": None,
          "byte_size": 101 * 1024 * 1024}, "byte_size_out_of_range"),
    ]:
        try:
            validate_media(**kwargs)
        except Exception as e:
            assert getattr(e, "code", None) == code
        else:
            raise AssertionError(f"应拒绝 {kwargs}")
