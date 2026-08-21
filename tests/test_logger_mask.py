"""日志脱敏测试：SensitiveFilter 必须屏蔽 Authorization / API Key，防止凭据泄漏进日志。

诊断导出会打包日志，脱敏是隐私底线，纳入 CI 门禁。
"""

from __future__ import annotations

import logging

from dsh_work.utils.logger import SensitiveFilter


def _apply_filter(msg: str) -> str:
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname=__file__, lineno=1,
        msg=msg, args=(), exc_info=None,
    )
    filter_ = SensitiveFilter()
    assert filter_.filter(record)
    return record.getMessage()


def test_masks_bearer_authorization() -> None:
    masked = _apply_filter("POST /api/chat Authorization: Bearer sk-abcdef1234567890")
    assert "sk-abcdef1234567890" not in masked
    assert "***MASKED***" in masked


def test_masks_api_key_assignment() -> None:
    masked = _apply_filter("using api_key=sk-12345678abcdefgh for request")
    assert "sk-12345678abcdefgh" not in masked
    assert "***" in masked


def test_masks_plain_sk_token() -> None:
    masked = _apply_filter("token sk-abcdef1234567890 appeared in payload")
    assert "sk-abcdef1234567890" not in masked


def test_keeps_normal_message_unchanged() -> None:
    msg = "session created successfully, 3 messages"
    assert _apply_filter(msg) == msg
