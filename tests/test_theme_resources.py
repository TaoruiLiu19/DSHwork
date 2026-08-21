"""内置主题资源测试：主题 JSON 必须可解析且包含渲染器所需的关键字段。

主题文件来自 dsh_work/resources/themes/，PyInstaller 打包时按 datas 打包，
运行时 theme_manager 解析。结构损坏会导致应用配色异常，故纳入 CI。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dsh_work import constants as C
from dsh_work.config import get_builtin_themes_dir

THEMES_DIR = Path(__file__).resolve().parent.parent / "dsh_work" / "resources" / "themes"

# theme_manager 渲染依赖的必需颜色键（与 QSS 生成强耦合）
REQUIRED_COLOR_KEYS = {
    "bg_primary",
    "bg_secondary",
    "bg_sidebar",
    "text_primary",
    "text_secondary",
    "text_muted",
    "accent",
    "accent_hover",
    "success",
    "warning",
    "error",
    "border",
    "input_bg",
    "input_border",
    "markdown_code_bg",
    "markdown_inline_bg",
}


def test_builtin_themes_declared_match_files() -> None:
    """constants.BUILTIN_THEMES 与 themes 目录下的 JSON 一一对应。"""
    on_disk = {p.stem for p in THEMES_DIR.glob("*.json")}
    assert set(C.BUILTIN_THEMES) == on_disk


def test_get_builtin_themes_dir_points_to_resources() -> None:
    """源码运行时 get_builtin_themes_dir() 应解析到 resources/themes。"""
    assert get_builtin_themes_dir().is_dir()
    assert (get_builtin_themes_dir() / f"{C.DEFAULT_THEME}.json").exists()


@pytest.mark.parametrize("theme_name", C.BUILTIN_THEMES)
def test_theme_json_structure(theme_name: str) -> None:
    path = THEMES_DIR / f"{theme_name}.json"
    data = json.loads(path.read_text(encoding="utf-8"))

    assert data.get("name"), "主题缺少 name"
    assert data.get("type") in {"dark", "light"}, "type 必须是 dark/light"

    colors = data.get("colors")
    assert isinstance(colors, dict), "缺少 colors 对象"
    missing = REQUIRED_COLOR_KEYS - set(colors)
    assert not missing, f"{theme_name} 缺少颜色键: {sorted(missing)}"

    # 主题类型必须与文件名语义一致（web_dark -> dark, web_light -> light）
    if "dark" in theme_name:
        assert data["type"] == "dark"
    if "light" in theme_name:
        assert data["type"] == "light"


@pytest.mark.parametrize("theme_name", C.BUILTIN_THEMES)
def test_theme_json_values_valid(theme_name: str) -> None:
    """颜色值必须是合法颜色字符串（hex / rgba），防止手误。"""
    import re

    path = THEMES_DIR / f"{theme_name}.json"
    data = json.loads(path.read_text(encoding="utf-8"))

    hex_color = re.compile(r"^#[0-9a-fA-F]{6}$")
    rgba_color = re.compile(r"^rgba?\(\s*\d+[\s,]*\d*[\s,]*\d*[\s,]*[\d.]*\s*\)$")

    for key, value in data["colors"].items():
        assert isinstance(value, str), f"{theme_name}.colors.{key} 不是字符串"
        assert hex_color.match(value) or rgba_color.match(value), (
            f"{theme_name}.colors.{key} 非法颜色值: {value!r}"
        )
