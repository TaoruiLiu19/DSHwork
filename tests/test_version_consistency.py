"""版本一致性测试：三个版本源必须同步。

单一来源原则（README 声称）：
- dsh_work/constants.py    APP_VERSION（运行时）
- pyproject.toml           project.version（分发元数据）
- installer/dsh-work.iss   MyAppVersion（Inno Setup 安装包）

任何一处漂移都会导致「安装包版本 ≠ 程序内部版本 ≠ PyPI 元数据」，
CI 用它作为门禁，防止发布错版本。
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from dsh_work import constants as C

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _iss_version() -> str:
    iss_path = PROJECT_ROOT / "installer" / "dsh-work.iss"
    text = iss_path.read_text(encoding="utf-8")
    match = re.search(r'#define MyAppVersion\s+"([^"]+)"', text)
    assert match, "dsh-work.iss 中找不到 MyAppVersion 定义"
    return match.group(1)


def _pyproject_version() -> str:
    with open(PROJECT_ROOT / "pyproject.toml", "rb") as f:
        data = tomllib.load(f)
    return data["project"]["version"]


def test_constants_version_matches_pyproject() -> None:
    assert C.APP_VERSION == _pyproject_version()


def test_constants_version_matches_iss() -> None:
    assert C.APP_VERSION == _iss_version()


def test_package_version_matches_constants() -> None:
    """dsh_work.__init__.__version__ 与 constants.APP_VERSION 一致。"""
    import dsh_work

    assert dsh_work.__version__ == C.APP_VERSION


@pytest.mark.parametrize(
    "version",
    [
        _iss_version(),
        _pyproject_version(),
        C.APP_VERSION,
    ],
)
def test_version_semver_shape(version: str) -> None:
    """版本号必须是 x.y.z 形态（Inno Setup / 更新器解析依赖）。"""
    assert re.fullmatch(r"\d+\.\d+\.\d+", version), f"版本号不是 x.y.z 形态: {version!r}"
