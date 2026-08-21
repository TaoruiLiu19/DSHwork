"""会话历史异步加载测试。

验证 switch_to 在无缓存历史时走后台线程加载（不阻塞主线程），
且 _on_history_fetched 正确更新 state 并触发 history_loaded 通知。
"""

from __future__ import annotations

import time


class _FakeDsh:
    """最小 DshService mock：只实现 switch_session / get_history / add_ws_handler。"""

    def __init__(self):
        self.switched: list[str] = []
        self.calls = 0
        self.history: list = []
        self.ws = type("WS", (), {"on_reconnected": None})()

    def switch_session(self, session_id: str) -> None:
        self.switched.append(session_id)

    def get_history(self, session_id: str, limit: int = 50) -> list:
        self.calls += 1
        time.sleep(0.05)  # 模拟 RPC 延迟
        return list(self.history)

    def add_ws_handler(self, handler) -> None:
        pass


def _make_manager():
    from dsh_work.core.session_manager import SessionManager

    dsh = _FakeDsh()
    mgr = SessionManager(dsh)
    return mgr, dsh


def _fake_message(i: int):
    from types import SimpleNamespace

    return SimpleNamespace(role="user" if i % 2 else "assistant", content=f"msg {i}", timestamp=float(i))


def test_switch_to_async_loads_history_in_background() -> None:
    """无缓存历史时：switch_to 立即返回（不阻塞），后台线程拉取后通知。"""
    mgr, dsh = _make_manager()
    dsh.history = [_fake_message(1), _fake_message(2)]

    events: list[str] = []
    mgr.add_listener(lambda sid, state: events.append(state.last_event_type))

    t0 = time.perf_counter()
    mgr.switch_to("sess-1")
    t1 = time.perf_counter()
    # switch_to 本身不等待 RPC（后台线程），应快速返回
    assert t1 - t0 < 0.05, f"switch_to 阻塞了 {((t1-t0)*1000):.0f}ms"

    # 等待后台线程完成
    deadline = time.time() + 3
    while not dsh.calls and time.time() < deadline:
        time.sleep(0.01)
    assert dsh.calls == 1, "后台线程应调用一次 get_history"
    assert dsh.switched == ["sess-1"]


def test_on_history_fetched_updates_state_and_notifies() -> None:
    """历史拉取完成后：state.messages 更新且发出 history_loaded 事件。"""
    mgr, dsh = _make_manager()
    dsh.history = [_fake_message(1), _fake_message(2)]

    events: list[str] = []
    mgr.add_listener(lambda sid, state: events.append(state.last_event_type))

    mgr.switch_to("sess-1")
    # 模拟桥接器在主线程调用 _on_history_fetched
    deadline = time.time() + 3
    while not dsh.calls and time.time() < deadline:
        time.sleep(0.01)
    mgr._on_history_fetched("sess-1", list(dsh.history))

    state = mgr._sessions["sess-1"]
    assert len(state.messages) == 2
    assert "history_loaded" in events, f"应发出 history_loaded 通知, got {events}"


def test_switch_to_same_session_noop() -> None:
    """重复切换同一会话不重复加载。"""
    mgr, dsh = _make_manager()
    mgr.switch_to("sess-1")
    mgr.switch_to("sess-1")
    # 第一次已触发异步加载；第二次直接 return，不新增调用
    time.sleep(0.2)
    assert len(dsh.switched) == 1
