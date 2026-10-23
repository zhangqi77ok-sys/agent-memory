"""Markdown 同步（第十三轮）：记忆以人可读文件为真相，DB/向量退为派生索引。

动因（GitHub 周榜对照，2026-10-23）：冲榜的 ai-memory 靠「纯 Markdown 存 git」做
跨工具记忆，行业在收敛到「人可读文件是真相、向量索引是派生层」。本模块把这条
落到本系统：按主题导出 .md（带条目 id），用户能在编辑器里看、改、diff；导回时
按 id 对齐应用编辑与新增。

安全边界（定死，不搞复杂双向合并）：
- 文件里**删掉**的条目只在报告里列出（missing_in_files），**绝不**据此删库——
  删错文件不等于要删记忆，真删除走既有的 forget/面板路径；
- 事实/偏好改内容走版本滚动（与面板「纠正」同语义：旧版失效、新版 +1）；
- 经验改内容就地更新并作废旧向量（与第十一轮 PATCH 同语义）；
- 所有回库内容先过 redact()，文件里手滑写进的密钥不落库。
"""
import hashlib
import re
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import models as m
from app.auth import UserContext
from app.redact import redact
from app.skills_export import _slug

_ID_RE = re.compile(r"<!--\s*id:([0-9a-f]{32})(?:\s+kind:(\w+))?\s*-->")
_FACT_LINE = re.compile(r"^- (.*?)\s*<!--\s*id:([0-9a-f]{32})\s*-->\s*$")
_PREF_LINE = re.compile(r"^- (.*?)\s*=\s*(.*?)\s*<!--\s*id:([0-9a-f]{32})\s*-->\s*$")


def _subject_files(rows, kind: str) -> dict[str, list]:
    by_subject: dict[str, list] = {}
    for r in rows:
        by_subject.setdefault(r.subject, []).append(r)
    used: dict[str, str] = {}
    out: dict[str, list] = {}
    for subject, items in by_subject.items():
        slug = _slug(subject)
        if slug in used and used[slug] != subject:
            slug = f"{slug}-{hashlib.sha1(subject.encode()).hexdigest()[:6]}"
        used[slug] = subject
        out[slug] = items
    return out


def render_memory_markdown(db: Session, u: UserContext) -> dict[str, str]:
    """导出为 {相对路径: Markdown 内容}：事实按主题、偏好单文件、经验按主题。"""
    mine = (m.MemoryFact.app_id == u.app_id, m.MemoryFact.user_id == u.user_id,
            m.MemoryFact.valid_to.is_(None))
    facts = list(db.scalars(select(m.MemoryFact).where(*mine)
                            .order_by(m.MemoryFact.subject, m.MemoryFact.valid_from)))
    prefs = list(db.scalars(select(m.MemoryPreference).where(
        m.MemoryPreference.app_id == u.app_id, m.MemoryPreference.user_id == u.user_id,
        m.MemoryPreference.valid_to.is_(None))))
    insights = list(db.scalars(select(m.MemoryInsight).where(
        m.MemoryInsight.app_id == u.app_id, m.MemoryInsight.user_id == u.user_id,
        m.MemoryInsight.valid_to.is_(None)).order_by(m.MemoryInsight.subject)))

    files: dict[str, str] = {}
    for slug, items in _subject_files(facts, "facts").items():
        subject = items[0].subject
        lines = ["---", "type: facts", f"subject: {subject}", "---", "",
                 f"# 事实 · {subject}", ""]
        for f in items:
            content = " ".join(f.content.split())
            lines.append(f"- {content} <!-- id:{f.id} -->")
        files[f"facts/{slug}.md"] = "\n".join(lines) + "\n"
    if prefs:
        lines = ["---", "type: preferences", "---", "", "# 偏好", ""]
        for p in prefs:
            lines.append(f"- {p.key} = {' '.join(p.value.split())} <!-- id:{p.id} -->")
        files["preferences.md"] = "\n".join(lines) + "\n"
    for slug, items in _subject_files(insights, "insights").items():
        subject = items[0].subject
        lines = ["---", "type: insights", f"subject: {subject}", "---", "",
                 f"# 经验 · {subject}", ""]
        for i in items:
            lines.append(f"<!-- id:{i.id} kind:{i.kind} -->")
            lines.append(i.content.strip())
            if i.applies_when:
                lines.append(f"适用：{i.applies_when}")
            lines.append("<!-- /id -->")
            lines.append("")
        files[f"insights/{slug}.md"] = "\n".join(lines) + "\n"
    return files


def _frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    out = {}
    for line in text[3:end].splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def parse_memory_markdown(files: dict[str, str]) -> list[dict]:
    """解析导出的 Markdown（容忍手改）：返回条目 dict 列表，id 可为空（新增）。"""
    items: list[dict] = []
    for path, text in files.items():
        fm = _frontmatter(text)
        ftype = fm.get("type", "")
        if ftype == "facts" or path.startswith("facts/"):
            subject = fm.get("subject", "")
            for line in text.splitlines():
                mm = _FACT_LINE.match(line.strip())
                if mm:
                    items.append({"type": "fact", "id": mm.group(2),
                                  "subject": subject, "content": mm.group(1).strip()})
                elif line.strip().startswith("- ") and "<!--" not in line:
                    items.append({"type": "fact", "id": None, "subject": subject,
                                  "content": line.strip()[2:].strip()})
        elif ftype == "preferences" or path.endswith("preferences.md"):
            for line in text.splitlines():
                mm = _PREF_LINE.match(line.strip())
                if mm:
                    items.append({"type": "preference", "id": mm.group(3),
                                  "key": mm.group(1).strip(), "value": mm.group(2).strip()})
                elif line.strip().startswith("- ") and "=" in line and "<!--" not in line:
                    k, v = line.strip()[2:].split("=", 1)
                    items.append({"type": "preference", "id": None,
                                  "key": k.strip(), "value": v.strip()})
        elif ftype == "insights" or path.startswith("insights/"):
            subject = fm.get("subject", "")
            blocks = re.split(r"<!--\s*/id\s*-->", text)
            for block in blocks:
                mm = _ID_RE.search(block)
                new_mm = None if mm else re.search(
                    r"<!--\s*new(?:\s+kind:(\w+))?\s*-->", block)
                if not mm and not new_mm:
                    continue
                mark = mm or new_mm
                body = block[mark.end():].strip()
                applies = ""
                blines = body.splitlines()
                if blines and blines[-1].startswith("适用："):
                    applies = blines[-1][3:].strip()
                    body = "\n".join(blines[:-1]).strip()
                items.append({"type": "insight",
                              "id": mm.group(1) if mm else None,
                              "kind": mark.group(2) if mm else (mark.group(1) or "lesson"),
                              "subject": subject,
                              "content": body, "applies_when": applies})
    return items


def apply_markdown_import(db: Session, u: UserContext, files: dict[str, str]) -> dict:
    """把文件侧的编辑/新增应用回库；删除只报告。自己提交（与 brief 同纪律）。"""
    from app.api.memories import _audit

    parsed = parse_memory_markdown(files)
    now = datetime.now(UTC)
    report = {"updated": 0, "created": 0, "unchanged": 0, "redacted": 0,
              "missing_in_files": []}
    seen_ids: set[str] = set()
    created_rows: list = []  # 本轮新建的条目不算「不在文件里」，算缺失前要排除

    def _clean(text: str) -> str:
        out = redact(text or "")
        if out != (text or ""):
            report["redacted"] += 1
        return out.strip()

    for it in parsed:
        if it["type"] == "fact":
            content = _clean(it["content"])
            if not content:
                continue
            row = db.get(m.MemoryFact, it["id"]) if it["id"] else None
            if row and row.user_id == u.user_id and row.app_id == u.app_id \
                    and row.valid_to is None:
                seen_ids.add(row.id)
                if row.content == content:
                    report["unchanged"] += 1
                else:
                    row.valid_to = now
                    db.add(m.MemoryFact(app_id=u.app_id, user_id=u.user_id,
                                        subject=row.subject, content=content,
                                        version=row.version + 1, valid_from=now))
                    report["updated"] += 1
            elif not it["id"]:
                row_new = m.MemoryFact(app_id=u.app_id, user_id=u.user_id,
                                       subject=(it["subject"] or "未分类")[:128],
                                       content=content, version=1, valid_from=now)
                db.add(row_new)
                created_rows.append(row_new)
                report["created"] += 1
        elif it["type"] == "preference":
            value = _clean(it["value"])
            row = db.get(m.MemoryPreference, it["id"]) if it["id"] else None
            if row and row.user_id == u.user_id and row.app_id == u.app_id \
                    and row.valid_to is None:
                seen_ids.add(row.id)
                if row.value == value:
                    report["unchanged"] += 1
                else:
                    row.valid_to = now
                    db.add(m.MemoryPreference(app_id=u.app_id, user_id=u.user_id,
                                              key=row.key, value=value,
                                              version=row.version + 1, valid_from=now))
                    report["updated"] += 1
            elif not it["id"]:
                row_new = m.MemoryPreference(app_id=u.app_id, user_id=u.user_id,
                                             key=(it["key"] or "未命名")[:128],
                                             value=value, version=1, valid_from=now)
                db.add(row_new)
                created_rows.append(row_new)
                report["created"] += 1
        elif it["type"] == "insight":
            content = _clean(it["content"])
            if not content:
                continue
            row = db.get(m.MemoryInsight, it["id"]) if it["id"] else None
            if row and row.user_id == u.user_id and row.app_id == u.app_id \
                    and row.valid_to is None:
                seen_ids.add(row.id)
                applies = _clean(it.get("applies_when", ""))
                if row.content == content and (row.applies_when or "") == applies:
                    report["unchanged"] += 1
                else:
                    row.content = content
                    row.applies_when = applies
                    row.embedding = None  # 匹配面变了，旧向量作废（第十一轮同语义）
                    row.embed_model = None
                    report["updated"] += 1
            elif not it["id"]:
                kind = it["kind"] if it["kind"] in (
                    "lesson", "strategy", "pitfall", "cost_saver") else "lesson"
                row_new = m.MemoryInsight(
                    app_id=u.app_id, user_id=u.user_id, kind=kind,
                    subject=(it["subject"] or "未分类")[:128], content=content,
                    applies_when=_clean(it.get("applies_when", "")))
                db.add(row_new)
                created_rows.append(row_new)
                report["created"] += 1
    if created_rows:
        db.flush()
        seen_ids.update(r.id for r in created_rows)
    # 删除只报告：现行条目里没在文件出现的 id 列出来，删不删由用户走正式路径决定
    for model in (m.MemoryFact, m.MemoryPreference, m.MemoryInsight):
        rows = db.scalars(select(model).where(
            model.app_id == u.app_id, model.user_id == u.user_id,
            model.valid_to.is_(None)))
        for r in rows:
            if r.id not in seen_ids:
                report["missing_in_files"].append(r.id)
    _audit(db, u, "md_import", [])
    db.commit()
    return report
