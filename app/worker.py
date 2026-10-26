"""写入 Worker：领取 memory_job，脱敏后抽取，再按代码规则入库。

领取仍是 SKIP LOCKED + 处理超时 + 最多 3 次重试。
抽取失败（网络/5xx）回到 pending，超过次数进 dead；模型输出不合法则丢弃该批并完成任务。
事实入库时同步生成 Gemini 向量（未配 GEMINI_API_KEY 或调用失败则留空，召回自动走关键词）。
缺 LLM 配置时进程直接退出，不用假抽取器。
"""
import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, text, update
from sqlalchemy.orm import Session

from app import models as m
from app.auth import UserContext
from app.db import WorkerSession
from app.embeddings import cosine
from app.extract import MAX_SUBJECT_LEN, PROMPT_VERSION, InvalidExtraction, decide, is_injection, norm, same_subject, strip_injection
from app.redact import mask_user_id, redact

MAX_ATTEMPTS = 3
LOCK_SECONDS = 120
# 语义去重阈值（第十八轮）：与经验合并的 MERGE_COSINE 同值——主题措辞不同、
# 但主题+内容向量近到这个程度，就是同一件事的不同说法，不该并存两行。
SEMANTIC_DUP_COSINE = 0.95
log = logging.getLogger("worker")


def claim_job(db: Session) -> m.MemoryJob | None:
    now = datetime.now(timezone.utc)
    q = (select(m.MemoryJob)
         .where(or_(m.MemoryJob.status == "pending",
                    (m.MemoryJob.status == "running") & (m.MemoryJob.locked_until < now)))
         .where(m.MemoryJob.attempts < MAX_ATTEMPTS)
         .order_by(m.MemoryJob.created_at)
         .limit(1)
         .with_for_update(skip_locked=True))
    job = db.scalar(q)
    if not job:
        return None
    job.status, job.attempts, job.locked_until = "running", job.attempts + 1, now + timedelta(seconds=LOCK_SECONDS)
    db.commit()
    return job


def renew_lease(db: Session, job_id: str) -> bool:
    """续租（修复第 10 轮，审计 F4）：单条原子 UPDATE，只续“仍 running 且
    租约未过期”的任务。已过期不续——过期意味着可能已被另一 worker 重领，
    旧持有者不得把租约抢回来。返回是否续上。"""
    now = datetime.now(timezone.utc)
    result = db.execute(
        update(m.MemoryJob)
        .where(m.MemoryJob.id == job_id,
               m.MemoryJob.status == "running",
               m.MemoryJob.locked_until > now)
        .values(locked_until=now + timedelta(seconds=LOCK_SECONDS)))
    db.commit()
    return result.rowcount == 1


class LeaseRenewer:
    """租约续租后台线程（修复第 10 轮，审计 F4 推荐方案 a）。

    处理耗时超过 LOCK_SECONDS 时，每 interval 秒用独立短会话续租一次；
    续不上（任务已 done/被删/租约已过期）即静默停线程，不干扰主处理。
    interval 默认 LOCK_SECONDS // 2（60s）。"""

    def __init__(self, db: Session, job_id: str, interval: float = LOCK_SECONDS // 2):
        self._engine = db.get_bind()
        self._job_id = job_id
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> "LeaseRenewer":
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        from sqlalchemy.orm import sessionmaker

        Session = sessionmaker(bind=self._engine)
        while not self._stop.wait(self._interval):
            try:
                with Session() as s:
                    if not renew_lease(s, self._job_id):
                        return
            except Exception:
                return  # 续租失败即停：任务按原租约过期后可被重领，不拖垮主处理

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)


def fail_job(db: Session, job: m.MemoryJob, err: str) -> None:
    job.last_error = err[:2000]
    job.status = "dead" if job.attempts >= MAX_ATTEMPTS else "pending"
    job.locked_until = None
    db.commit()


def expire_memories(db: Session, ttl_days: int = 0, now: datetime | None = None) -> int:
    """定期将超过保留时长的有效记忆标记为过期失效（valid_to = now）。

    返回标记失效的条数。
    """
    if ttl_days <= 0:
        return 0
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=ttl_days)
    count = 0
    # 修复第 4 轮：巡检开关过滤——显式关掉记忆的用户（§5 口径）不碰，
    # 哪怕是 TTL 到期也不由 worker 动他的数据；重新开启后下一轮恢复。
    from app.api.me import switched_off_users

    off = switched_off_users(db)

    def keep(r):
        return (r.app_id, r.user_id) not in off

    # 事实过期
    facts = [f for f in db.scalars(select(m.MemoryFact).where(
        m.MemoryFact.valid_to.is_(None),
        m.MemoryFact.valid_from < cutoff,
    )).all() if keep(f)]
    for f in facts:
        f.valid_to = now
        count += 1

    # 偏好过期
    prefs = [p for p in db.scalars(select(m.MemoryPreference).where(
        m.MemoryPreference.valid_to.is_(None),
        m.MemoryPreference.valid_from < cutoff,
    )).all() if keep(p)]
    for p in prefs:
        p.valid_to = now
        count += 1

    # 经验过期（第七轮补：新表此前不在 TTL 覆盖内，只进不出）
    insights = [i for i in db.scalars(select(m.MemoryInsight).where(
        m.MemoryInsight.valid_to.is_(None),
        m.MemoryInsight.valid_from < cutoff,
    )).all() if keep(i)]
    for ins in insights:
        ins.valid_to = now
        count += 1

    # 轨迹过期：原始过程比经验更敏感，不做软失效，直接删行（已提炼的经验按上一条独立过期）
    old_episodes = [e for e in db.scalars(select(m.TaskEpisode).where(
        m.TaskEpisode.created_at < cutoff,
    )).all() if keep(e)]
    for ep in old_episodes:
        db.delete(ep)
        count += 1

    if count > 0:
        db.commit()
        log.info("expired memories count=%s ttl_days=%s", count, ttl_days)
    return count



def _call_extractor(extractor, text: str):
    fn = extractor.extract if hasattr(extractor, "extract") else extractor
    return list(fn(text))


def _memory_on(db: Session, job: m.MemoryJob) -> bool:
    from app.api.me import memory_enabled

    return memory_enabled(db, UserContext(app_id=job.app_id, user_id=job.user_id))


def _messages(db: Session, job: m.MemoryJob) -> list[m.SessionMessage]:
    return list(db.scalars(select(m.SessionMessage).where(
        m.SessionMessage.job_id == job.id,
        m.SessionMessage.app_id == job.app_id,
        m.SessionMessage.user_id == job.user_id,
    ).order_by(m.SessionMessage.created_at)))


def _render(messages: list[m.SessionMessage]) -> str:
    return "\n".join(f"[{msg.role}] {redact(msg.content)}" for msg in messages)


def _source_msg_id(messages: list[m.SessionMessage]) -> str | None:
    user_msgs = [msg for msg in messages if msg.role == "user"]
    picked = (user_msgs or messages)
    return picked[-1].msg_id if picked else None


def _finish(db: Session, job: m.MemoryJob, decision: str, fact_ids: list[str]) -> str:
    job.status = "done"
    job.locked_until = None
    job.last_error = None
    db.add(m.AuditLog(
        app_id=job.app_id, user_id=job.user_id, action="write",
        fact_ids=fact_ids, decision=decision,
    ))
    db.commit()
    log.info("job=%s user=%s decision=%s n=%s prompt=%s", job.id, mask_user_id(job.user_id), decision, len(fact_ids), PROMPT_VERSION)
    return decision


def _active_value(rows: list, content: str, attr: str) -> str | None:
    if not rows:
        return None
    if any(norm(getattr(row, attr)) == norm(content) for row in rows):
        return content
    return getattr(rows[0], attr)


def _embed_fact(embedder, fact: m.MemoryFact) -> None:
    """给一条新事实补向量。失败只记警告：向量是增强项，不能拖垮写入任务。"""
    try:
        vec = embedder.embed([f"{fact.subject} {fact.content}"], task_type="RETRIEVAL_DOCUMENT")[0]
    except Exception as e:
        log.warning("embed fact=%s failed: %s", fact.id, e)
        return
    fact.embedding = vec
    fact.embed_model = getattr(embedder, "model", None)


def _embed_preference(embedder, pref: m.MemoryPreference) -> None:
    """给一条新偏好补向量（第十八轮，迁移 0018 加列后才有地方存）。
    偏好此前没有向量，语义去重在偏好侧只能退回词面——补齐后与事实同口径。"""
    try:
        vec = embedder.embed([f"{pref.key} {pref.value}"], task_type="RETRIEVAL_DOCUMENT")[0]
    except Exception as e:
        log.warning("embed preference=%s failed: %s", pref.id, e)
        return
    pref.embedding = vec
    pref.embed_model = getattr(embedder, "model", None)


def _semantic_ids(db: Session, model, job: m.MemoryJob, text: str, embedder) -> list[str]:
    """语义分支（第十八轮）：词面 same_subject 抓不到的跨措辞重复，靠向量抓。
    新文本现场嵌一次（与 _embed_fact 同口径 RETRIEVAL_DOCUMENT），与现行行里
    已存的同模型向量逐一比余弦，≥ SEMANTIC_DUP_COSINE 即同一件事。没 embedder、
    嵌失败、无存量向量时静默回落词面——语义是加分项，不是写入的前置条件。"""
    if embedder is None or not text:
        return []
    try:
        vec = embedder.embed([text], task_type="RETRIEVAL_DOCUMENT")[0]
    except Exception:
        return []
    if not vec:
        return []
    model_name = getattr(embedder, "model", None)
    rows = db.execute(select(model.id, model.embedding, model.embed_model).where(
        model.app_id == job.app_id,
        model.user_id == job.user_id,
        model.valid_to.is_(None),
        model.embedding.is_not(None),
    )).all()
    qvec = [float(x) for x in vec]
    out = []
    for rid, emb, em in rows:
        if model_name is not None and em is not None and em != model_name:
            continue
        try:
            if cosine([float(x) for x in emb], qvec) >= SEMANTIC_DUP_COSINE:
                out.append(rid)
        except (TypeError, ValueError):
            continue
    return out


def _matched_facts(db: Session, job: m.MemoryJob, subject: str, content: str = "",
                   embedder=None) -> list[m.MemoryFact]:
    """找与 subject 说同一件事的有效事实。先只查 (id, subject) 轻列做相似匹配，
    命中的才整行加载——不再把该用户的全部事实拉进内存逐条比。
    相似合并（same_subject）顺带治旧账：历史上字面不同、意思相同的重复行会在下一次写入时被一并失效。
    第十八轮补语义分支：主题措辞不同但意思相同（向量 ≥0.95）的行同样命中，
    之后走同一套 NOOP/UPDATE 判定——内容相同是佐证，内容不同是新版本。"""
    light = db.execute(select(m.MemoryFact.id, m.MemoryFact.subject).where(
        m.MemoryFact.app_id == job.app_id,
        m.MemoryFact.user_id == job.user_id,
        m.MemoryFact.valid_to.is_(None),
    )).all()
    ids = {fid for fid, subj in light if same_subject(subj, subject)}
    ids.update(_semantic_ids(db, m.MemoryFact, job, f"{subject} {content}".strip(), embedder))
    if not ids:
        return []
    return list(db.scalars(select(m.MemoryFact).where(m.MemoryFact.id.in_(sorted(ids)))))


def _matched_preferences(db: Session, job: m.MemoryJob, key: str, value: str = "",
                         embedder=None) -> list[m.MemoryPreference]:
    light = db.execute(select(m.MemoryPreference.id, m.MemoryPreference.key).where(
        m.MemoryPreference.app_id == job.app_id,
        m.MemoryPreference.user_id == job.user_id,
        m.MemoryPreference.valid_to.is_(None),
    )).all()
    ids = {pid for pid, k in light if same_subject(k, key)}
    ids.update(_semantic_ids(db, m.MemoryPreference, job, f"{key} {value}".strip(), embedder))
    if not ids:
        return []
    return list(db.scalars(select(m.MemoryPreference).where(m.MemoryPreference.id.in_(sorted(ids)))))


def support_confidence(support_count: int) -> float:
    """佐证置信度（第十七轮）：support/(support+1)——1 次 0.5、2 次 0.67、3 次 0.75，
    随独立佐证次数单调上升、渐近于 1。与经验的拉普拉斯置信同哲学：证据计数说话。"""
    s = max(int(support_count or 1), 1)
    return s / (s + 1)


def fact_confidence(support_count: int, feedback_pos: int = 0, feedback_neg: int = 0) -> float:
    """事实/偏好置信度（第二十三轮）：(S+P)/(S+P+N+1)，S = 佐证计数、P = 人工好评、
    N = 人工差评——即佐证公式的拉普拉斯推广。无反馈时恒等于 support_confidence
    （(S+0)/(S+0+0+1) = S/(S+1)），旧行数值不动；好评如一次新佐证，差评拉低，
    正负相抵回中性。佐证与反馈共用此式，两处口径不再打架。"""
    s = max(int(support_count or 1), 1)
    p = max(int(feedback_pos or 0), 0)
    n = max(int(feedback_neg or 0), 0)
    return (s + p) / (s + p + n + 1)


def _bump_support(rows: list, content: str, attr: str) -> None:
    """NOOP 的另一面（第十七轮）：内容被再次独立抽到 = 一次新佐证。
    此前 NOOP 直接丢弃，同一事实被说十次和被说一次在库里毫无区别；
    现在给命中的现行行记 support_count+1 并重算置信度（不新建版本，
    内容没变就不该有版本噪音）。第二十三轮：重算用 fact_confidence——
    反馈后若再被佐证，旧 support_confidence 会把负反馈洗掉；无反馈时
    两式同值，旧行为不变。"""
    for row in rows:
        if norm(getattr(row, attr)) == norm(content):
            row.support_count = (row.support_count or 1) + 1
            row.confidence = fact_confidence(row.support_count,
                                             row.feedback_pos or 0, row.feedback_neg or 0)


def _apply_fact(db: Session, job: m.MemoryJob, subject: str, content: str, proposal: str,
                source_msg_id: str | None, embedder=None) -> tuple[str, str | None]:
    active = _matched_facts(db, job, subject, content, embedder)
    decision = decide(_active_value(active, content, "content"), content, proposal=proposal)
    if decision == "NOOP":
        _bump_support(active, content, "content")
        return "noop", None
    now = datetime.now(timezone.utc)
    closed_ids = []
    for row in active:
        row.valid_to = now
        closed_ids.append(row.id)
    version = max((row.version for row in active), default=0) + 1
    created = m.MemoryFact(
        app_id=job.app_id, user_id=job.user_id, subject=subject, content=content,
        version=version, valid_from=now, source_session_id=job.session_id, source_msg_id=source_msg_id,
    )
    db.add(created)
    db.flush()
    if embedder is not None:
        _embed_fact(embedder, created)
    # 织网（第十五轮）：旧版本的关联边迁到新版本（矛盾边在迁移里关掉）
    from app.links import migrate_links

    for old_id in closed_ids:
        migrate_links(db, job.app_id, job.user_id, "fact", old_id, created.id)
    return decision.lower(), created.id


def _apply_preference(db: Session, job: m.MemoryJob, key: str, value: str, proposal: str,
                      embedder=None) -> tuple[str, str | None]:
    active = _matched_preferences(db, job, key, value, embedder)
    decision = decide(_active_value(active, value, "value"), value, proposal=proposal)
    if decision == "NOOP":
        _bump_support(active, value, "value")
        return "noop", None
    now = datetime.now(timezone.utc)
    for row in active:
        row.valid_to = now
    version = max((row.version for row in active), default=0) + 1
    created = m.MemoryPreference(
        app_id=job.app_id, user_id=job.user_id, key=key, value=value,
        version=version, valid_from=now,
    )
    db.add(created)
    db.flush()
    if embedder is not None:
        _embed_preference(embedder, created)
    return decision.lower(), created.id


def _apply(db: Session, job: m.MemoryJob, candidate, source_msg_id: str | None, embedder=None) -> tuple[str, str | None]:
    subject = redact(candidate.subject).strip()
    content = redact(candidate.content).strip()
    if not subject or not content or len(subject) > MAX_SUBJECT_LEN or is_injection(subject) or is_injection(content):
        return "noop", None
    # 修复第 2 轮：内容墓碑检查放在 NOOP bump 之前——旧任务重抽已遗忘内容
    # 时，若只拦 ADD/UPDATE 不拦 NOOP，support 会被悄悄刷到重建的行上。
    from app.tombstone import block_if_tombstoned

    if block_if_tombstoned(db, job.app_id, job.user_id, candidate.kind, subject, content):
        return "tombstoned", None
    if candidate.kind == "preference":
        return _apply_preference(db, job, subject, content, candidate.action, embedder=embedder)
    return _apply_fact(db, job, subject, content, candidate.action, source_msg_id, embedder=embedder)


def _summarize(decisions: list[str]) -> str:
    if "update" in decisions:
        return "update"
    if "add" in decisions:
        return "add"
    if decisions and all(d == "tombstoned" for d in decisions):
        return "tombstoned"
    return "noop"


def _lock_user(db: Session, job: m.MemoryJob) -> None:
    """同一用户的写入串行化。job 领取的 SKIP LOCKED 只锁任务行，两个 Worker 并发处理
    同一用户时 _apply 的读-改-写会竞态出双 active。Postgres 用事务级咨询锁兜底
    （随 commit/rollback 自动释放）；SQLite 单写者天然串行，跳过。"""
    bind = db.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k)::bigint)"),
                   {"k": f"{job.app_id}:{job.user_id}"})


def _midflight_gate(db: Session, job: m.MemoryJob) -> str | None:
    """在途任务提交前三重门（修复第 4 轮，审计 F1a/F1b/F1c 的剩余口子）。

    抽取/嵌入是慢调用，起手门（第 2 轮）看不见窗口内发生的状态变更，
    故每次写入前重查——三次查询均为新鲜 SELECT，不用 ORM 缓存对象
    （另一会话的提交只有新 SELECT 才看得见；SQLite 按提交态可见、
    PG 按 READ COMMITTED 语句快照，两库都成立）。
    返回 None = 放行；"missing" = 任务行已被删（clear_all 等）；
    "skip" = 开关已关，或用户在任务创建后又有新的遗忘（水位规则与
    第 2 轮起手门同一口径：整单丢弃，不论候选是否与遗忘内容相关——
    抽取输出措辞可能与被遗忘内容不同，逐候选哈希拦不住语义复活）。
    """
    from app.tombstone import _aware, user_forget_watermark

    if db.scalar(select(m.MemoryJob.id).where(m.MemoryJob.id == job.id)) is None:
        return "missing"
    if not _memory_on(db, job):
        return "skip"
    watermark = user_forget_watermark(db, job.app_id, job.user_id)
    if watermark is not None and _aware(job.created_at) is not None \
            and _aware(job.created_at) < watermark:
        log.info("job=%s user=%s skip: forgotten during extraction",
                 job.id, mask_user_id(job.user_id))
        return "skip"
    return None


def _extract_and_write(db: Session, job: m.MemoryJob, extractor, embedder=None,
                       linker=None) -> tuple[str, list[str]]:
    if not _memory_on(db, job):
        return "skip", []
    # 修复第 2 轮 (c)：创建于用户最近一次删除之前的旧任务整单丢弃——
    # 删除窗口内的在途任务即使抽的是无关内容也不写库，调用方须重发。
    # 这是 fail-closed 取舍：正常新任务（创建于删除之后）不受影响。
    from app.tombstone import _aware, user_forget_watermark

    watermark = user_forget_watermark(db, job.app_id, job.user_id)
    if watermark is not None and _aware(job.created_at) is not None \
            and _aware(job.created_at) < watermark:
        log.info("job=%s user=%s skip: created before forget watermark",
                 job.id, mask_user_id(job.user_id))
        return "skip", []
    _lock_user(db, job)
    messages = _messages(db, job)
    text = strip_injection(_render(messages)).strip()
    if not text:
        return "noop", []
    candidates = _call_extractor(extractor, text)
    source_msg_id = _source_msg_id(messages)
    decisions: list[str] = []
    ids: list[str] = []
    # 修复第 4 轮：抽取返回后先过一次门——候选为空时循环不跑，若不查，
    # _finish 会对已被删的任务行 UPDATE 0 行（SQLite 崩溃的直接触发点）。
    gate = _midflight_gate(db, job)
    if gate is not None:
        db.rollback()
        return gate, []
    for candidate in candidates:
        # 嵌入调用也可能耗时，逐候选再查一次；命中则回滚本批待定写入，
        # 不留半批（_finish 只提交审计与任务终态，事实写回已被回滚）。
        gate = _midflight_gate(db, job)
        if gate is not None:
            db.rollback()
            return gate, []
        decision, new_id = _apply(db, job, candidate, source_msg_id, embedder=embedder)
        decisions.append(decision)
        if new_id:
            ids.append(new_id)
            if decision in ("add", "update") and candidate.kind != "preference":
                # 织网（第十五轮）：新事实落库即连边
                from app.links import weave_on_write

                fact_row = db.get(m.MemoryFact, new_id)
                if fact_row is not None:
                    weave_on_write(db, job.app_id, job.user_id, fact_row, linker=linker)
    return _summarize(decisions), ids


def process_job(db: Session, job: m.MemoryJob, extractor, embedder=None, linker=None) -> str:
    """处理一条已领取的任务。成功、跳过、丢弃非法输出都把 status 置为 done。可重试错误向外抛。"""
    job_id = job.id
    current = db.scalar(select(m.MemoryJob).where(m.MemoryJob.id == job_id).with_for_update())
    if current is None:
        return "missing"
    if current.status == "done":
        db.commit()
        return "done"
    try:
        decision, fact_ids = _extract_and_write(db, current, extractor, embedder=embedder,
                                                linker=linker)
    except InvalidExtraction:
        db.rollback()
        current = db.scalar(select(m.MemoryJob).where(m.MemoryJob.id == job_id).with_for_update())
        # 修复第 4 轮：重读为空 = 任务行在抽取中途被删（clear_all），
        # 是正常终态不是故障；此前直接 current.user_id 会 AttributeError。
        if current is None:
            return "missing"
        log.info("job=%s user=%s decision=discard prompt=%s", job_id, mask_user_id(current.user_id), PROMPT_VERSION)
        return _finish(db, current, "discard", [])
    except Exception:
        db.rollback()
        raise
    # 修复第 4 轮：任务行已删时不调 _finish——行已无，无可更新，
    # 调了就是 UPDATE 0 行的 StaleDataError（SQLite 崩溃的触发点）。
    if decision == "missing":
        db.rollback()
        return "missing"
    return _finish(db, current, decision, fact_ids)


def run_once(db: Session, extractor, embedder=None, linker=None,
             lease_interval: float | None = None) -> m.MemoryJob | None:
    """领一条并处理。没有待处理任务时返回 None。失败写入 last_error，超限后 status=dead。
    lease_interval 非空时处理期间起续租线程（修复第 10 轮；测试默认不续租，
    主循环传 LOCK_SECONDS // 2）。"""
    job = claim_job(db)
    if not job:
        return None
    job_id = job.id
    renewer = LeaseRenewer(db, job_id, interval=lease_interval).start() \
        if lease_interval else None
    try:
        process_job(db, job, extractor, embedder=embedder, linker=linker)
    except Exception as e:
        db.rollback()
        failed = db.get(m.MemoryJob, job_id)
        if failed is None:
            # 修复第 4 轮：任务行在处理中途被删是正常终态（clear_all），
            # 不是故障；此前重抛 → main 主循环无 try → worker 进程退出。
            log.warning("job=%s deleted mid-flight, dropping", job_id)
            return None
        fail_job(db, failed, str(e))
        log.warning("job=%s attempts=%s status=%s error=%s", job_id, failed.attempts, failed.status, failed.last_error)
    finally:
        if renewer is not None:
            renewer.stop()
    return db.get(m.MemoryJob, job_id)


def _post_run_wait(job, ep) -> float:
    """主循环一轮结束后的等待秒数（纯函数，可测试）。

    - job 仍 pending：指数退避，上限 30s；
    - 其余（无 job / job 已终态）：固定 2s，避免空转。
    """
    if job is not None and job.status == "pending":
        return float(min(2 ** max(job.attempts, 1), 30))
    return 2.0


PATROL_INTERVAL_SECONDS = 3600  # 巡检节拍：成功与失败都按此间隔推后（失败不再热重试）


def run_patrol(db: Session, linker=None, ttl_days: int = 0,
               skeleton_days: int = 90) -> dict:
    """巡检五步（worker 每小时节拍），各步内部分片提交。
    抢不到全局巡检锁时整轮跳过，返回 {"skipped": "patrol_lock"}（另一副本
    正在跑，不是失败）。"""
    from app.patrol import release_patrol_lock, try_patrol_lock

    if not try_patrol_lock(db):
        log.warning("patrol skipped: another worker holds the patrol lock")
        return {"skipped": "patrol_lock"}
    try:
        stats: dict = {}
        if ttl_days > 0:
            stats["expired"] = expire_memories(db, ttl_days)
        # 经验生命周期巡检与 TTL 同一个小时节拍（第十一轮）：衰减/合并/矛盾计数
        from app.episodes import consolidate_insights
        cstats = consolidate_insights(db, linker=linker)
        stats["consolidate"] = cstats
        if cstats["decayed"] or cstats["merged"] or cstats["conflicts"] \
                or cstats["synthesized"]:
            log.info("consolidate_insights: %s", cstats)
        # 织网巡检（第十五轮）：补边、矛盾落边、跨项目抽象，与生命周期同节拍
        from app.links import weave_links
        wstats = weave_links(db, linker=linker)
        stats["weave"] = wstats
        if wstats["linked"] or wstats["generalized"] or wstats["pruned"]:
            log.info("weave_links: %s", wstats)
        # 睡眠巩固（第二十一轮）：从使用证据重组——共用强化/建边、
        # 死边剪除、事实合并、共用簇抽象；各项自带门限，每小时跑安全
        from app.sleep import sleep_consolidate
        sstats = sleep_consolidate(db, linker=linker)
        stats["sleep"] = sstats
        if any(sstats[k] for k in ("merged", "pruned", "strengthened",
                                   "linked", "generalized")):
            log.info("sleep_consolidate: %s", sstats)
        # 轨迹骨架化（第十七轮）：提炼完的旧轨迹只留骨架，与巡检同节拍
        from app.episodes import skeletonize_episodes
        sk = skeletonize_episodes(db, retention_days=skeleton_days)
        stats["skeletonized"] = sk
        if sk:
            log.info("skeletonize_episodes: %s", sk)
        return stats
    finally:
        release_patrol_lock(db)


def run_cycle(db: Session, extractor, embedder, linker, distiller, patrol_state: dict,
              lease_interval: float | None = None) -> dict:
    """一轮主循环（修复第 10 轮，审计 F2）：先处理任务，后跑巡检。
    patrol_state: {"next_patrol": float}——到点才跑巡检；巡检成功或失败都把
    next_patrol 推后一小时（失败不再 2s 热重试，且失败不阻塞下一循环的
    任务处理——任务在本函数前半已处理）。返回 {"job", "episode", "patrol"}，
    patrol 为 None 表示本轮未到巡检点。"""
    from app.episodes import run_episode_once

    job = run_once(db, extractor, embedder=embedder, linker=linker,
                   lease_interval=lease_interval)
    ep = None
    for _ in range(5):  # 一轮最多连提 5 条：历史回放批量导入时不至于一条一轮慢慢磨（第八轮补）
        ep = run_episode_once(db, distiller, embedder=embedder, linker=linker)
        if ep is None:
            break
    patrol = None
    if time.time() >= patrol_state["next_patrol"]:
        try:
            from app.config import settings
            patrol = run_patrol(db, linker=linker, ttl_days=settings.memory_ttl_days,
                                skeleton_days=settings.episode_skeleton_days)
        except Exception as e:
            log.warning("cleanup error: %s", e)
            patrol = {"error": str(e)}
        patrol_state["next_patrol"] = time.time() + PATROL_INTERVAL_SECONDS
    return {"job": job, "episode": ep, "patrol": patrol}


def main() -> None:  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    from app.distill import build_distiller
    from app.embeddings import build_embedder
    from app.extract import build_extractor

    extractor = build_extractor()
    distiller = build_distiller()
    embedder = build_embedder()
    from app.links import build_linker

    linker = build_linker()  # 未配 LLM 时为 None：规则织网照常，语义判定缺席
    log.info("worker started prompt=%s embedding=%s linker=%s", PROMPT_VERSION,
             embedder.model if embedder is not None else "off",
             "llm" if linker is not None else "rule")
    patrol_state = {"next_patrol": 0.0}
    while True:
        with WorkerSession() as db:
            result = run_cycle(db, extractor, embedder, linker, distiller, patrol_state,
                               lease_interval=LOCK_SECONDS // 2)
        time.sleep(_post_run_wait(result["job"], result["episode"]))


if __name__ == "__main__":  # pragma: no cover
    main()
