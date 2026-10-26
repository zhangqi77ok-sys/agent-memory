#!/usr/bin/env python3
"""前瞻记忆向量路精度度量（第三十一轮）：第 27 轮只量了关键词路，本脚本补向量路。

背景：第 27 轮出厂口径（关键词路）precision 0.833 / recall 0.714 / FPR 0.143，
两例换说法漏触与一例高重叠异意图误触被判定为“关键词无解、留给向量路”，
但向量路到底能不能解从未跑过数字。本脚本用同一批标签（14 组探针、6 条
常驻型提醒）跑三臂对照：关键词路 / 向量路 / 联合生产路，并做向量阈值扫掠。

生产保真关键点：提醒向量路是非对称的——条件入库时按 RETRIEVAL_DOCUMENT
嵌，任务触发时按 RETRIEVAL_QUERY 嵌（app/reminders.py）。连接器自带的
embed.py 单次调用只能用一种 task 类型、且只输出成对余弦，量不出生产分数；
本脚本内置一个鸭子类型适配器，实现 GeminiEmbedder 协议
（embed(texts, task_type) + .model，dim 取生产默认 1536），逐条调
embedContent，凭证走连接器自己的 dynamic_credentials 代理流程（与 embed.py
同款：add_surrogate_to_request，allowed_hosts 仅 generativelanguage
.googleapis.com）——真实 key 永不出现、不进代码、不进日志、不进报告。

运行环境声明：适配器只在有 Muse Gemini 连接器代理的环境可用（本 VM）；
不可用时脚本干净失败并明说，不伪造数据。本脚本是度量 harness，不是
生产代码；回归测试用 FakeEmbedder，不依赖网络。

度量方法：
- 临时 SQLite 库上跑，不碰生产库；提醒一律常驻型（once=False），探针互不消耗。
- 关键词臂：check_triggers(embedder=None)，必须复现第 27 轮 5/1/2/6，
  否则度量口径已漂移，结论作废。
- 向量臂：入库时存的条件 DOCUMENT 向量 × 任务 QUERY 向量余弦矩阵，
  per-case 只看本条件是否触发；不混关键词。
- 联合臂：真实 check_triggers + 适配器（出厂行为）；另以余弦矩阵校验
  0.66 档的联合数与真实跑一致（本批各条件动作互不相同，去重不干扰）。
- 扫掠：复用余弦矩阵，零额外调用，阈值 0.50 / 0.60 / 0.66 / 0.75 / 0.80。

诚实边界：
- 14 组探针是方向性样本，不是标定级样本；数字不外推为生产误触率。
- 提醒触发线自第九轮起已与召回线拆分（app/recall.py 的 COS_MIN_REMINDER，
  初始值 0.66，行为不变）；本脚本仍以 0.66 档度量提醒臂，即提醒线初始值，
  数字与第 31 轮可比。仅凭本批数字不移动任何一侧的线；
  采纳门槛见设计草稿 round31-design-draft.md。
- API 量：6 条条件 DOCUMENT + 14 个任务 QUERY = 20 次单条调用。

用法：
  python3 scripts/reminder_vector_precision.py
"""

import json
import os
import sys
import tempfile
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(tempfile.mkdtemp(prefix="reminder-vector-precision-"), "test.db")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("SERVICE_KEYS", "reminder-vector-precision")

sys.path.insert(0, "/opt/muse/skills/custom-connector-setup/bin")
try:
    from dynamic_credentials import add_surrogate_to_request, read_json_response
except ImportError as e:  # pragma: no cover - 环境缺连接器代理时干净失败
    raise SystemExit(f"无 Muse Gemini 连接器代理（dynamic_credentials 不可用）：{e}") from e

import app.models  # noqa: F401  (建表需注册模型类)
import app.reminders as reminders
from app.auth import UserContext
from app.config import settings
from app.db import Base, SessionLocal, engine
from app.embeddings import cosine
from tests.test_round27_reminders import REMINDER_CASES, REMINDERS

CREDENTIAL = "custom.google-gemini-api"
HOST = "generativelanguage.googleapis.com"
URL = f"https://{HOST}/v1beta/models/gemini-embedding-001:embedContent"
MODEL = "gemini-embedding-001"
DIM = 1536  # 生产默认（app/config.py embedding_dim）
SWEEP = (0.50, 0.60, 0.66, 0.75, 0.80)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise RuntimeError("redirect refused, verify the endpoint")


class SurrogateEmbedder:
    """鸭子类型适配器：实现 GeminiEmbedder 协议，走连接器代理流程取真实向量。

    与 app.embeddings.GeminiEmbedder 的区别仅在取 key 的方式：这里不读
    GEMINI_API_KEY 环境变量（本 VM 没有），而是复用连接器 helper 的代理
    机制；真实 key 由 authd 只对已注册主机换出，本脚本全程见不到它。
    """

    model = MODEL

    def __init__(self, dim: int = DIM, timeout: float = 60.0):
        self.dim = dim
        self.timeout = timeout

    def embed(self, texts, task_type="RETRIEVAL_DOCUMENT"):
        return [self._embed_one(t, task_type) for t in texts]

    def _embed_one(self, text, task_type):
        payload = {
            "content": {"parts": [{"text": text}]},
            "taskType": task_type,
            "outputDimensionality": self.dim,
        }
        req = urllib.request.Request(
            URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        add_surrogate_to_request(req, CREDENTIAL, allowed_hosts=[HOST])
        try:
            with urllib.request.build_opener(_NoRedirect()).open(req, timeout=self.timeout) as resp:
                body = read_json_response(resp)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:300]
            raise RuntimeError(f"embedding HTTP {e.code}: {detail}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"embedding 连接失败：{e.reason}") from e
        vec = body["embedding"]["values"]
        if len(vec) != self.dim:
            raise RuntimeError(f"embedding 维度异常：{len(vec)} != {self.dim}")
        return vec


def _confusion(rows):
    tp = fp = fn = tn = 0
    for _cond, _task, want, got in rows:
        if want and got:
            tp += 1
        elif want and not got:
            fn += 1
        elif not want and got:
            fp += 1
        else:
            tn += 1
    return tp, fp, fn, tn


def _fmt(tp, fp, fn, tn):
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    fpr = fp / (fp + tn) if fp + tn else 0.0
    return f"TP={tp} FP={fp} FN={fn} TN={tn} precision={prec:.3f} recall={rec:.3f} FPR={fpr:.3f}"


def main():
    emb = SurrogateEmbedder()
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        u = UserContext(app_id=settings.app_id, user_id="alice")
        for cond, action in REMINDERS:
            reminders.create_reminder(db, u, condition=cond, action=action, once=False, embedder=emb)
        # 入库时存的条件 DOCUMENT 向量：直接读库，不重复调用。
        stored = {}
        for row in reminders.list_reminders(db, u):
            stored[row.condition_text] = [float(x) for x in row.embedding]
        assert len(stored) == len(REMINDERS), f"条件向量缺失：{len(stored)}"
        # 任务 QUERY 向量：14 次调用。
        tasks = [task for _c, _a, task, _w in REMINDER_CASES]
        qvecs = emb.embed(tasks, task_type="RETRIEVAL_QUERY")

        # 关键词臂（对照组）与联合臂（真实 check_triggers）。
        kw_rows, comb_rows = [], []
        for cond, _action, task, want in REMINDER_CASES:
            fired = reminders.check_triggers(db, u, task, embedder=None)
            kw_rows.append((cond, task, want, any(r.condition_text == cond for r in fired)))
            fired = reminders.check_triggers(db, u, task, embedder=emb)
            comb_rows.append((cond, task, want, any(r.condition_text == cond for r in fired)))

        # 余弦矩阵：行 = case，列 = 本条件 cos。
        cos_rows = []
        for (cond, _action, task, want), qv in zip(REMINDER_CASES, qvecs, strict=True):
            cos_rows.append((cond, task, want, cosine(qv, stored[cond])))

    print(f"model={MODEL} dim={DIM} cases={len(REMINDER_CASES)}")
    print("keyword-only (control, must reproduce round27 5/1/2/6):")
    print("  " + _fmt(*_confusion(kw_rows)))
    print("vector-only @COS_MIN=0.66 (own-condition cosine):")
    vec_rows = [(c, t, w, cos >= 0.66) for c, t, w, cos in cos_rows]
    print("  " + _fmt(*_confusion(vec_rows)))
    print("combined production (real check_triggers + embedder):")
    print("  " + _fmt(*_confusion(comb_rows)))
    print("\nper-case (want / kw / cos / vec@0.66 / combined):")
    for (cond, task, want, cos), (_c2, _t2, _w2, kw_got), (_c3, _t3, _w3, comb_got) in zip(cos_rows, kw_rows, comb_rows, strict=True):
        vec_got = cos >= 0.66
        mark = "OK  " if want == comb_got else ("FP  " if comb_got else "FN  ")
        print(f"{mark} want={int(want)} kw={int(kw_got)} cos={cos:.3f} vec={int(vec_got)} comb={int(comb_got)} cond={cond!r} task={task!r}")
    # 矩阵校验：0.66 档联合数必须等于真实跑，否则口径不一致。
    comb_matrix = [(c, t, w, kw or cos >= 0.66) for (c, t, w, cos), (_c, _t, _w, kw) in zip(cos_rows, kw_rows, strict=True)]
    assert _confusion(comb_matrix) == _confusion(comb_rows), "矩阵联合数与真实 check_triggers 不一致，口径有问题"
    print("\nmatrix check @0.66: combined counts match real check_triggers run")
    print("\nthreshold sweep (guard fixed at shipped 0.5; vector-only / combined):")
    for t in SWEEP:
        v_rows = [(c, task, w, cos >= t) for c, task, w, cos in cos_rows]
        c_rows = [(c, task, w, kw or cos >= t) for (c, task, w, cos), (_c, _t, _w, kw) in zip(cos_rows, kw_rows, strict=True)]
        print(f"  {t:.2f}: vector [{_fmt(*_confusion(v_rows))}]  combined [{_fmt(*_confusion(c_rows))}]")


if __name__ == "__main__":
    main()
