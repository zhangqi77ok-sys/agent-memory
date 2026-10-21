"""规则脱敏。送给模型之前、写入事实/偏好之前都要走这里。

只替换明显的联系方式和证件号，不试图理解语义。占位符固定，方便后面核对「原文没进模型和库」。
"""
import re

# 顺序：邮箱 → 身份证 → 手机号 → 银行卡。身份证是 18 位，必须先于「长数字当卡号」拿掉。
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_CN_ID = re.compile(
    r"(?<!\d)\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)"
)
_PHONE = re.compile(r"(?<!\d)(?:\+?86[\s\-]?)?1[3-9](?:[\s\-]?\d){9}(?!\d)")
_CARD = re.compile(r"(?<!\d)(?:\d[ \-]?){13,19}(?!\d)")


def redact(text: str) -> str:
    if not text:
        return text
    text = _EMAIL.sub("[邮箱]", text)
    text = _CN_ID.sub("[身份证]", text)
    text = _PHONE.sub("[手机号]", text)
    text = _CARD.sub("[银行卡]", text)
    return text
