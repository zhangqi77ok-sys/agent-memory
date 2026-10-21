import time
from collections import defaultdict
from threading import Lock

from app.config import Settings, settings


class SlidingWindowRateLimiter:
    """基于滑动窗口的按 key (user_id) 限流器，线程安全。"""

    def __init__(self, max_requests: int, window_seconds: float = 60.0):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._history: dict[str, list[float]] = defaultdict(list)
        self._lock = Lock()

    def is_allowed(self, key: str) -> tuple[bool, float]:
        """检查 key 是否允许请求。

        返回 (is_allowed, retry_after_seconds)。
        """
        if self.max_requests <= 0:
            return True, 0.0
        now = time.time()
        with self._lock:
            history = self._history[key]
            cutoff = now - self.window_seconds
            while history and history[0] <= cutoff:
                history.pop(0)

            if len(history) >= self.max_requests:
                retry_after = max(1.0, history[0] + self.window_seconds - now)
                return False, retry_after

            history.append(now)
            return True, 0.0

    def reset(self) -> None:
        with self._lock:
            self._history.clear()


class UserRateLimiter:
    """应用层按 user_id 区分读写强度的复合限流器。"""

    def __init__(self, s: Settings = settings):
        self.enabled = s.is_rate_limit_enabled
        self.read_limiter = SlidingWindowRateLimiter(s.rate_limit_read_per_min, window_seconds=60.0)
        self.write_limiter = SlidingWindowRateLimiter(s.rate_limit_write_per_min, window_seconds=60.0)

    @staticmethod
    def is_strict_endpoint(path: str, method: str) -> bool:
        """写接口、导出接口与删除纠正等高敏感接口限流更严。"""
        m = method.upper()
        # 写入任务
        if path == "/v1/memories" and m == "POST":
            return True
        # 导出全部数据
        if path == "/v1/me/memories/export":
            return True
        # 全部清空
        if path == "/v1/me/memories" and m == "DELETE":
            return True
        # 纠正或删除单条
        if path.startswith("/v1/me/memories/") and m in ("DELETE", "PATCH"):
            return True
        # 修改设置
        if path == "/v1/me/settings" and m == "PUT":
            return True
        return False

    def check(self, user_id: str, path: str, method: str) -> tuple[bool, float]:
        if not self.enabled or not user_id:
            return True, 0.0
        if self.is_strict_endpoint(path, method):
            return self.write_limiter.is_allowed(user_id)
        return self.read_limiter.is_allowed(user_id)

    def reset(self) -> None:
        self.read_limiter.reset()
        self.write_limiter.reset()
