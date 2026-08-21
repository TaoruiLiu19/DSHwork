"""StatsDock 统计条（对齐 Web 版 sessionStats 投影）。

显示当前会话的运行统计（数据源：session.list 的 projections.values）：
- Turn N · Step M
- 上下文 X%（contextPressure.pressureTokens / contextWindow）
- 本轮输入/输出 tokens（tokenUsage）
- LLM 耗时 / 工具耗时（sessionStats.llmMs / toolMs）

无数据时自动隐藏；配色走主题 token（StatsLabel）。
"""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

from ...utils.logger import get_logger

log = get_logger("ui.stats_dock")


def _fmt_ms(ms: float) -> str:
    try:
        ms = float(ms)
    except (TypeError, ValueError):
        return ""
    if ms <= 0:
        return ""
    if ms < 1000:
        return f"{int(ms)}ms"
    if ms < 60000:
        return f"{ms / 1000:.1f}s"
    return f"{ms / 60000:.1f}m"


def _fmt_tokens(n) -> str:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


class StatsDock(QWidget):
    """会话运行统计条（Web 版 stats line）。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("StatsDock")
        self._labels: list[QLabel] = []
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(16, 2, 16, 2)
        layout.setSpacing(14)
        self._parts: dict[str, QLabel] = {}
        for key in ("turns", "context", "tokens", "timing"):
            lbl = QLabel()
            lbl.setObjectName("StatsLabel")
            lbl.setVisible(False)
            self._parts[key] = lbl
            layout.addWidget(lbl)
            self._labels.append(lbl)
        layout.addStretch()
        self.setVisible(False)

    def clear(self) -> None:
        """无会话时隐藏全部统计。"""
        for lbl in self._labels:
            lbl.setVisible(False)
        self.setVisible(False)

    def set_projections(self, projections: dict) -> None:
        """从 session.list 的 projections.values 更新统计。"""
        if not isinstance(projections, dict) or not projections:
            self.clear()
            return

        stats = projections.get("sessionStats") if isinstance(projections.get("sessionStats"), dict) else {}
        pressure = projections.get("contextPressure") if isinstance(projections.get("contextPressure"), dict) else {}
        tokens = projections.get("tokenUsage") if isinstance(projections.get("tokenUsage"), dict) else {}

        parts_text: list[tuple[str, str]] = []

        turns = stats.get("turns", 0)
        steps = stats.get("steps", 0)
        if turns or steps:
            parts_text.append(("turns", f"Turn {turns} · Step {steps}"))

        pressure_tokens = pressure.get("pressureTokens", 0)
        context_window = pressure.get("contextWindow", 0)
        if pressure_tokens and context_window:
            pct = int(pressure_tokens * 100 / context_window)
            parts_text.append(("context", f"上下文 {pct}%"))

        in_tok = tokens.get("uncachedInputTokens", 0)
        out_tok = tokens.get("outputTokens", 0)
        if in_tok or out_tok:
            parts_text.append(("tokens", f"输入 {_fmt_tokens(in_tok)} · 输出 {_fmt_tokens(out_tok)}"))

        llm_ms = stats.get("llmMs", 0)
        tool_ms = stats.get("toolMs", 0)
        timing = []
        if llm_ms:
            timing.append(f"LLM {_fmt_ms(llm_ms)}")
        if tool_ms:
            timing.append(f"工具 {_fmt_ms(tool_ms)}")
        if timing:
            parts_text.append(("timing", " · ".join(timing)))

        any_visible = False
        for key, text in parts_text:
            lbl = self._parts.get(key)
            if lbl is not None:
                lbl.setText(text)
                lbl.setVisible(True)
                any_visible = True
        for key, lbl in self._parts.items():
            if key not in [k for k, _ in parts_text]:
                lbl.setVisible(False)
        self.setVisible(any_visible)
