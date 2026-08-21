"""附件上传功能测试。

回归：📎 附件按钮此前没有连接点击处理（点击无反应），
且发送时 _attached_files 从未上报给外部（附件从未随消息发出）。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.usefixtures("qapp")


def _make_input_box():
    from dsh_work.ui.widgets.input_box import InputBox

    return InputBox()


def test_attach_button_is_connected() -> None:
    """📎 附件按钮必须有点击连接（此前未连接，点击无反应）。"""
    box = _make_input_box()
    assert box._attach_btn is not None
    # 点击应触发 _on_attach_clicked 而不会抛异常（用 monkeypatch 验证信号链）
    from PySide6.QtWidgets import QFileDialog

    called = []

    def fake_picker(*args, **kwargs):
        called.append(True)
        return [], ""

    original = QFileDialog.getOpenFileNames
    QFileDialog.getOpenFileNames = staticmethod(fake_picker)
    try:
        box._attach_btn.click()
        assert called, "📎 按钮点击应打开文件选择对话框"
    finally:
        QFileDialog.getOpenFileNames = original


def test_attach_picker_adds_files(monkeypatch) -> None:
    """点击 📎 选择图片：加入附件列表 + 计数标签显示。"""
    from PySide6.QtWidgets import QFileDialog

    box = _make_input_box()

    def fake_picker(*args, **kwargs):
        return ["C:/tmp/photo.png"], ""

    monkeypatch.setattr(QFileDialog, "getOpenFileNames", staticmethod(fake_picker))
    box._on_attach_clicked()
    assert len(box._attached_files) == 1
    assert box._attach_label.isVisibleTo(box), "附件计数标签应显示"
    assert "1" in box._attach_label.text()


def test_attach_image_does_not_insert_path_text(monkeypatch) -> None:
    """图片附件不插入路径文本（避免路径文本 + image block 重复）。"""
    from PySide6.QtWidgets import QFileDialog

    box = _make_input_box()

    def fake_picker(*args, **kwargs):
        return ["C:/tmp/photo.png"], ""

    monkeypatch.setattr(QFileDialog, "getOpenFileNames", staticmethod(fake_picker))
    box._on_attach_clicked()
    assert "photo.png" not in box._text_edit.toPlainText(), "图片路径不应插入文本"


def test_attach_non_image_inserts_path_text(monkeypatch) -> None:
    """非图片文件路径插入文本（发送时按图片过滤，文本保留可用）。"""
    from PySide6.QtWidgets import QFileDialog

    box = _make_input_box()

    def fake_picker(*args, **kwargs):
        return ["C:/tmp/notes.txt"], ""

    monkeypatch.setattr(QFileDialog, "getOpenFileNames", staticmethod(fake_picker))
    box._on_attach_clicked()
    assert "notes.txt" in box._text_edit.toPlainText(), "非图片路径应插入文本"


def test_send_reports_unreported_attachments() -> None:
    """发送时通过 files_dropped 上报附件（供外部随消息发送）。"""
    from dsh_work.ui.widgets.input_box import InputBox

    box = InputBox()
    reported: list = []
    sent: list = []
    box.files_dropped.connect(reported.append)
    box.send_requested.connect(sent.append)

    # 模拟点击 📎 添加图片 + 输入文本
    box._attached_files = ["C:/tmp/photo.png"]
    box._reported_files = set()
    box._text_edit.setPlainText("看图")
    box._on_send()

    assert reported == [["C:/tmp/photo.png"]], f"应上报附件, got {reported}"
    assert sent == ["看图"]
    assert box._attached_files == [], "发送后附件应清空"


def test_send_does_not_repeat_dropped_attachments() -> None:
    """拖拽已上报的附件，发送时不重复上报（避免 _pending_attachments 重复）。"""
    from dsh_work.ui.widgets.input_box import InputBox

    box = InputBox()
    reported: list = []
    box.files_dropped.connect(reported.append)

    # 模拟拖拽：已上报 + 已记录
    box._attached_files = ["C:/tmp/drag.png"]
    box._reported_files = {"C:/tmp/drag.png"}
    box._text_edit.setPlainText("内容")
    box._on_send()

    assert reported == [], f"已上报过的附件不应重复上报, got {reported}"


def test_image_content_block_conversion(tmp_path) -> None:
    """图片附件转 session.prompt image 块（base64）；非图片返回 None。"""
    from dsh_work.api.dsh_service import DshService

    # 1x1 PNG
    png = tmp_path / "pixel.png"
    png.write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
            "1f15c4890000000d49444154789c6360000002000100ffff0300000600"
            "0557bfabd40000000049454e44ae426082"
        )
    )
    block = DshService._image_content_block(str(png))
    assert block is not None
    assert block["type"] == "image"
    assert block["mediaType"] == "image/png"
    assert block["data"], "图片应转 base64"

    # 非图片 → None
    txt = tmp_path / "note.txt"
    txt.write_text("hi")
    assert DshService._image_content_block(str(txt)) is None


def test_send_message_builds_text_and_image_blocks(tmp_path) -> None:
    """send_message 构建 content 块：文本 + 图片块；非图片被忽略。"""
    from dsh_work.api.dsh_service import DshService

    png = tmp_path / "pixel.png"
    png.write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
            "1f15c4890000000d49444154789c6360000002000100ffff0300000600"
            "0557bfabd40000000049454e44ae426082"
        )
    )
    txt = tmp_path / "note.txt"
    txt.write_text("hi")

    service = DshService.__new__(DshService)  # 不触发 __init__

    class _FakeHttp:
        def __init__(self):
            self.calls = []

        def call(self, method, params):
            self.calls.append((method, params))
            return {"accepted": True}

    http = _FakeHttp()
    service.http = http
    service.send_message("sess-1", "看图", attachments=[str(png), str(txt)])

    assert len(http.calls) == 1
    method, payload = http.calls[0]
    assert method == "session.prompt"
    blocks = payload["content"]
    types = [b["type"] for b in blocks]
    assert types == ["text", "image"], f"应只含文本+图片块, got {types}"
    assert blocks[0]["text"] == "看图"
    assert blocks[1]["mediaType"] == "image/png"
