"""规则脱敏。送给模型之前、写入事实/偏好之前都要走这里。

只替换明显的联系方式和证件号，不试图理解语义。占位符固定，方便后面核对「原文没进模型和库」。
"""
import hashlib
import logging
import re

# 顺序：邮箱 → 身份证 → 手机号 → 银行卡。身份证是 18 位，必须先于「长数字当卡号」拿掉。
# 密钥形态（轨迹、报错、命令里最容易夹带的东西）。只认已知形状，不猜任意长串：
# 任意密钥若长得不像下面任何一种，仍可能漏过——这是规则脱敏的固有边界，评测集里有案可查。
_SECRET_SUBS = [
    (re.compile(r"sk-[A-Za-z0-9_\-]{10,}"), "[密钥]"),  # OpenAI 风格 API key
    (re.compile(r"AIza[A-Za-z0-9_\-]{10,}"), "[密钥]"),  # Google API key
    (re.compile(r"ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"), "[密钥]"),  # GitHub 令牌
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[密钥]"),  # AWS access key id
    (re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), "[令牌]"),  # JWT
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}"), "Bearer [令牌]"),
    (re.compile(r"://[^\s/:@]+:[^\s/@]+@"), "://[账号口令]@"),  # 连接串/URL 里的口令
    (re.compile(r"(?i)\b(api[_-]?key|secret|password|passwd|token)\b\s*[:=]\s*[\"']?[^\s\"',;]+"),
     r"\1=[已脱敏]"),  # 键值对形态：password=... / api_key: ...
]

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_CN_ID = re.compile(
    r"(?<!\d)\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)"
)
_PHONE = re.compile(r"(?<!\d)(?:\+?86[\s\-]?)?1[3-9](?:[\s\-]?\d){9}(?!\d)")
_CARD = re.compile(r"(?<!\d)(?:\d[ \-]?){13,19}(?!\d)")

_USER_ID_QUERY = re.compile(r"((?:^|[?&]|\b)user_id=)([^& \t\r\n\"']+)")


def mask_user_id(user_id: str | None) -> str:
    """脱敏 user_id：用 sha256 取前 8 位短哈希，例如 usr#a1b2c3d4。应用日志和 access log 统一使用。"""
    if not user_id:
        return ""
    h = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:8]
    return f"usr#{h}"


def redact_query_string(query_or_url: str) -> str:
    """将 query string 或 url 中的 user_id=<val> 替换为 user_id=<mask_user_id(val)>。"""
    if not query_or_url:
        return query_or_url
    return _USER_ID_QUERY.sub(lambda m: m.group(1) + mask_user_id(m.group(2)), query_or_url)


class RedactUserIdFilter(logging.Filter):
    """日志过滤器：对日志记录中的明文 user_id 进行脱敏处理，防止泄露。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            new_args = []
            for arg in record.args:
                if isinstance(arg, str) and "user_id=" in arg:
                    new_args.append(redact_query_string(arg))
                else:
                    new_args.append(arg)
            record.args = tuple(new_args)
        elif isinstance(record.args, dict):
            new_dict = {}
            for k, v in record.args.items():
                if isinstance(v, str) and "user_id=" in v:
                    new_dict[k] = redact_query_string(v)
                else:
                    new_dict[k] = v
            record.args = new_dict

        if isinstance(record.msg, str) and "user_id=" in record.msg:
            record.msg = redact_query_string(record.msg)
        return True


def setup_logging_filters() -> None:
    """为 uvicorn.access 以及 root logger 添加 RedactUserIdFilter，确保日志中不出现明文 user_id。"""
    f = RedactUserIdFilter()
    logging.getLogger("uvicorn.access").addFilter(f)
    logging.getLogger("uvicorn").addFilter(f)
    logging.getLogger().addFilter(f)


def redact(text: str) -> str:
    if not text:
        return text
    for pattern, repl in _SECRET_SUBS:  # 密钥先行：里面常有长数字段，免得被银行卡规则拆碎漏检
        text = pattern.sub(repl, text)
    text = _EMAIL.sub("[邮箱]", text)
    text = _CN_ID.sub("[身份证]", text)
    text = _PHONE.sub("[手机号]", text)
    text = _CARD.sub("[银行卡]", text)
    return text
