from collections import defaultdict
from datetime import datetime, timezone
from threading import Lock

from sqlalchemy import text


class MetricsCollector:
    """采集并暴露 Prometheus 监控指标。"""

    def __init__(self):
        self._lock = Lock()
        self._request_counts: dict[tuple[str, str, str], int] = defaultdict(int)
        self._5xx_count: int = 0

    def record_request(self, method: str, path: str, status_code: int) -> None:
        clean_path = self._normalize_path(path)
        status_str = str(status_code)
        with self._lock:
            self._request_counts[(method.upper(), clean_path, status_str)] += 1
            if status_code >= 500:
                self._5xx_count += 1

    @staticmethod
    def _normalize_path(path: str) -> str:
        p = path.rstrip("/") or "/"
        if p.startswith("/memory/"):
            p = p[len("/memory"):]
        if p.startswith("/v1/me/memories/"):
            return "/v1/me/memories/{item_id}"
        return p

    def collect_job_metrics(self, engine) -> tuple[dict[str, int], float]:
        """统计当前 memory_job 各状态任务数量与最旧 pending 任务的年龄（秒）。"""
        counts = {"pending": 0, "running": 0, "done": 0, "dead": 0}
        oldest_age = 0.0
        now = datetime.now(timezone.utc)
        try:
            with engine.connect() as c:
                rows = c.execute(text("SELECT status, count(*) FROM memory_job GROUP BY status")).all()
                for status, cnt in rows:
                    if status in counts:
                        counts[status] = int(cnt)
                    else:
                        counts[str(status)] = int(cnt)

                oldest_created = c.execute(
                    text("SELECT min(created_at) FROM memory_job WHERE status = 'pending'")
                ).scalar()
                if oldest_created:
                    if isinstance(oldest_created, str):
                        try:
                            oldest_dt = datetime.fromisoformat(oldest_created)
                        except Exception:
                            oldest_dt = now
                    else:
                        oldest_dt = oldest_created
                    if oldest_dt.tzinfo is None:
                        oldest_dt = oldest_dt.replace(tzinfo=timezone.utc)
                    oldest_age = max(0.0, (now - oldest_dt).total_seconds())
        except Exception:
            pass
        return counts, oldest_age

    def check_db_ready(self, engine) -> int:
        try:
            with engine.connect() as c:
                c.execute(text("SELECT 1"))
            return 1
        except Exception:
            return 0

    def generate_metrics_text(self, engine) -> str:
        job_counts, oldest_age = self.collect_job_metrics(engine)
        db_ready = self.check_db_ready(engine)

        with self._lock:
            req_items = list(self._request_counts.items())
            c5xx = self._5xx_count

        lines = [
            "# HELP http_requests_total Total number of HTTP requests processed.",
            "# TYPE http_requests_total counter",
        ]
        for (m, p, s), count in req_items:
            lines.append(f'http_requests_total{{method="{m}",path="{p}",status="{s}"}} {count}')

        lines.extend([
            "# HELP http_requests_5xx_total Total number of HTTP 5xx server error responses.",
            "# TYPE http_requests_5xx_total counter",
            f"http_requests_5xx_total {c5xx}",
            "# HELP memory_job_count Current memory jobs count grouped by status.",
            "# TYPE memory_job_count gauge",
        ])
        for st, cnt in sorted(job_counts.items()):
            lines.append(f'memory_job_count{{status="{st}"}} {cnt}')

        lines.extend([
            "# HELP memory_job_oldest_pending_age_seconds Age of oldest pending memory job in seconds.",
            "# TYPE memory_job_oldest_pending_age_seconds gauge",
            f"memory_job_oldest_pending_age_seconds {oldest_age:.2f}",
            "# HELP memory_readyz_status Database readiness probe status (1=ready, 0=unhealthy).",
            "# TYPE memory_readyz_status gauge",
            f"memory_readyz_status {db_ready}",
            "",
        ])
        return "\n".join(lines)

    def reset(self) -> None:
        with self._lock:
            self._request_counts.clear()
            self._5xx_count = 0


metrics_collector = MetricsCollector()
