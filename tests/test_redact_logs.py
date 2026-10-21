import logging

from app.redact import RedactUserIdFilter, mask_user_id, redact_query_string


def test_mask_user_id():
    assert mask_user_id("") == ""
    assert mask_user_id(None) == ""
    m1 = mask_user_id("alice")
    m2 = mask_user_id("alice")
    assert m1.startswith("usr#")
    assert m1 == m2
    assert "alice" not in m1


def test_redact_query_string():
    assert redact_query_string("") == ""
    raw = "/v1/me/memories?user_id=secret-alice-123&kind=fact"
    redacted = redact_query_string(raw)
    assert "secret-alice-123" not in redacted
    assert "user_id=usr#" in redacted
    assert "&kind=fact" in redacted

    raw_single = "user_id=bob"
    assert "bob" not in redact_query_string(raw_single)
    # url with &
    raw_multi = "/export?offset=0&user_id=charlie&limit=10"
    redacted_multi = redact_query_string(raw_multi)
    assert "charlie" not in redacted_multi
    assert "user_id=usr#" in redacted_multi


def test_redact_user_id_logging_filter():
    f = RedactUserIdFilter()
    logger = logging.getLogger("test_redact_logger")
    logger.setLevel(logging.INFO)
    logger.addFilter(f)

    # 捕获日志记录
    records = []

    class CapturingHandler(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = CapturingHandler()
    logger.addHandler(handler)

    logger.info("Access: GET /v1/me/memories?user_id=plaintext-user-456 HTTP/1.1")
    logger.info('127.0.0.1 - "%s %s HTTP/1.1" 200', "GET", "/v1/me/memories?user_id=another-plain-user")

    assert len(records) == 2

    # 验证明文 user_id 不在 msg 或 formatted args 里
    msg0 = records[0].getMessage()
    assert "plaintext-user-456" not in msg0
    assert "user_id=usr#" in msg0

    msg1 = records[1].getMessage()
    assert "another-plain-user" not in msg1
    assert "user_id=usr#" in msg1
