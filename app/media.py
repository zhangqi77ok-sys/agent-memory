"""多模态入库（第 40 轮）：截图 / 语音复盘以文本层进记忆。

诚实边界（详见 round40-design-draft.md）：
- 本机没有 vision API、没有语音转录服务；服务端不解析像素、不转录音频。
  文本层一律由调用方提供（客户端 OCR 结果、用户手写描述、外部转录文本），
  origin 只做标注，不做可信度加权——服务端无法验证 OCR/转录质量。
- 媒体备忘 = 标准 TaskEpisode（source=screenshot/voice-memo）+ 一行来源
  sidecar（memory_media_ref）。文本层经 distill 提炼成 insight 后自然进入
  brief，不新增召回臂、不动任何排序与阈值。
- 原文件字节永不入库：服务端只存调用方申报的元数据（文件名、mime、
  大小、sha256、外部指针），且永不拉取 external_uri（只存字符串）。
"""

import uuid

from sqlalchemy import select

from app import models as m
from app.episodes import submit_episode
from app.redact import redact
from app.tombstone import is_episode_tombstoned

MEDIA_KINDS = ("screenshot", "voice")
MEDIA_ORIGINS = ("ocr", "transcript", "note", "external")
IMAGE_MIMES = ("image/png", "image/jpeg", "image/webp", "image/gif")
AUDIO_MIMES = ("audio/mpeg", "audio/wav", "audio/ogg", "audio/mp4", "audio/webm")
ALLOWED_MIMES = {"screenshot": IMAGE_MIMES, "voice": AUDIO_MIMES}
SOURCE_BY_KIND = {"screenshot": "screenshot", "voice": "voice-memo"}
STEP_ACTION = {"screenshot": "截图内容", "voice": "语音复盘"}

MAX_TEXT_CHARS = 24000  # 与轨迹渲染上限同口径（episodes.MAX_RENDER_CHARS）
MAX_CHUNK = 2000  # 单步 observation 上限（与 EpisodeStep 同口径）
MAX_STEPS = 200
MAX_BYTE_SIZE = 100 * 1024 * 1024  # 申报值 sanity check；服务端不收字节
TRUNCATION_NOTE = "（文本层超长，已截断）"


class MediaError(ValueError):
    """条目级错误：校验 / 墓碑 / 白名单问题。code 进批量结果，不抛整批。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def validate_media(kind: str, origin: str, text: str, mime: str | None,
                   byte_size: int | None) -> None:
    """纯校验：kind/origin 枚举、文本层非空、mime 白名单、申报大小上限。"""
    if kind not in MEDIA_KINDS:
        raise MediaError("invalid_kind", f"kind 非法：{kind!r}，只接受 screenshot/voice")
    if origin not in MEDIA_ORIGINS:
        raise MediaError("invalid_origin", f"origin 非法：{origin!r}")
    if not text or not text.strip():
        raise MediaError("empty_text_layer", "文本层不能为空（服务端不解析像素/音频，不替调用方猜）")
    if len(text) > MAX_TEXT_CHARS:
        raise MediaError("text_too_long", f"文本层超长：{len(text)} 字，上限 {MAX_TEXT_CHARS}")
    if mime is not None and mime not in ALLOWED_MIMES[kind]:
        raise MediaError("unsupported_media_type", f"mime 不在 {kind} 白名单内：{mime!r}")
    if byte_size is not None and (byte_size < 0 or byte_size > MAX_BYTE_SIZE):
        raise MediaError("byte_size_out_of_range", f"申报大小超限：{byte_size}")


def chunk_text_layer(text: str) -> list[str]:
    """文本层切块：先按行切，再把超长行硬切成 ≤MAX_CHUNK 的块。

    空行跳过（不占步数）；步数上限 MAX_STEPS，超了截断并在最后一步
    追加截断标记——不静默丢尾巴。
    """
    chunks: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        while len(line) > MAX_CHUNK:
            chunks.append(line[:MAX_CHUNK])
            line = line[MAX_CHUNK:]
        if line:
            chunks.append(line)
    if len(chunks) > MAX_STEPS:
        chunks = chunks[:MAX_STEPS]
        chunks[-1] = chunks[-1] + TRUNCATION_NOTE
    return chunks


def build_media_steps(kind: str, text: str) -> list[dict]:
    """文本层 → episode steps：每步 action 固定前缀，observation 承载文本块。

    脱敏前置：切块前整段过 redact（submit_episode 入库时还会再脱敏一次，
    与第 39 轮 capture 同款纵深）。
    """
    redacted = redact(text)
    action = STEP_ACTION[kind]
    return [{"action": action, "observation": c} for c in chunk_text_layer(redacted)]


def _tombstoned(db, u, title: str, steps: list[dict]) -> bool:
    """墓碑检查：两种入库形态都查（第 39 轮教训——REST 经 model_dump 带 None
    键、MCP/capture 的 dict 不带，只查一种会漏拦另一通道的已遗忘轨迹）。"""
    red_title = redact(title)
    full = [{k: (redact(v) if isinstance(v, str) else v) for k, v in s.items()}
            for s in steps]
    dropped = [{k: v for k, v in s.items() if v is not None} for s in full]
    return (is_episode_tombstoned(db, u.app_id, u.user_id, red_title, dropped)
            or is_episode_tombstoned(db, u.app_id, u.user_id, red_title, full))


def submit_media_memo(db, u, *, kind: str, origin: str, title: str, text: str,
                      outcome: str = "partial", project: str | None = None,
                      idempotency_key: str | None = None,
                      filename: str | None = None, mime: str | None = None,
                      byte_size: int | None = None, sha256: str | None = None,
                      external_uri: str | None = None) -> tuple[m.TaskEpisode, m.MemoryMediaRef | None]:
    """落一条媒体备忘：episode + sidecar 同一 session 同一事务。

    顺序：校验 → 墓碑检查（默认就查，无 opt-out）→ sidecar 行挂进 session
    → submit_episode（其内部 commit 一并提交两者）。submit_episode 抛错
    时回滚连 sidecar 一起清，不留孤儿引用。幂等键命中已有 episode 时
    跳过 sidecar 重建，返回 (已有 episode, 已有 sidecar 或 None)。
    返回 (episode, sidecar)：sidecar 为 None 只发生在幂等命中且旧提交
    本就没有 sidecar 的情形（第 40 轮前的老 episode 不可能有 sidecar）。
    """
    validate_media(kind, origin, text, mime, byte_size)
    if outcome not in ("success", "failure", "partial"):
        raise MediaError("invalid_outcome", f"outcome 非法：{outcome!r}")
    steps = build_media_steps(kind, text)
    if _tombstoned(db, u, title, steps):
        raise MediaError("tombstoned", "该内容已被遗忘，禁写")
    if idempotency_key:
        from app.episodes import find_episode_by_key

        existing = find_episode_by_key(db, u.app_id, u.user_id, idempotency_key)
        if existing is not None:
            ref = db.scalar(select(m.MemoryMediaRef).where(
                m.MemoryMediaRef.app_id == u.app_id,
                m.MemoryMediaRef.user_id == u.user_id,
                m.MemoryMediaRef.episode_id == existing.id))
            return existing, ref
    episode_id = uuid.uuid4().hex
    ref = m.MemoryMediaRef(
        app_id=u.app_id, user_id=u.user_id, episode_id=episode_id,
        kind=kind, origin=origin,
        filename=(filename[:256] if filename else None),
        mime=mime, byte_size=byte_size,
        sha256=(sha256[:64] if sha256 else None),
        external_uri=(external_uri[:512] if external_uri else None))
    db.add(ref)
    ep = submit_episode(db, u, source=SOURCE_BY_KIND[kind], title=title,
                        outcome=outcome, steps=steps, cost={},
                        idempotency_key=idempotency_key, project=project,
                        episode_id=episode_id)
    return ep, ref


def submit_media_batch(db, u, items: list[dict]) -> list[dict]:
    """批量提交：逐条独立 try/except，一条失败只记该条 error，不中断整批。

    每条结果：{"index": i, "ok": True, "episode_id": ...} 或
    {"index": i, "ok": False, "error": code}。整批接口本身只对认证/
    同意门失败返回错误码，条目级失败一律进结果数组。
    """
    results: list[dict] = []
    for i, item in enumerate(items):
        try:
            ep, _ref = submit_media_memo(db, u, **item)
            results.append({"index": i, "ok": True, "episode_id": ep.id})
        except MediaError as e:
            db.rollback()
            results.append({"index": i, "ok": False, "error": e.code})
        except ValueError as e:
            # submit_episode 的完整性校验（标题/步骤非法）也是条目级失败
            db.rollback()
            results.append({"index": i, "ok": False, "error": str(e)})
    return results
