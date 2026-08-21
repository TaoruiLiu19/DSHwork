"""配置层测试：UserConfig 序列化往返、损坏回退、旧主题迁移、WS URL 协议映射。

所有测试通过 monkeypatch 把 `get_user_config_path` 指向 tmp_path，
避免污染真实用户目录 ~/.dsh-work/。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dsh_work import config


@pytest.fixture()
def isolated_config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把配置读写重定向到临时目录，返回该路径。"""
    path = tmp_path / "config.json"
    monkeypatch.setattr(config, "get_user_config_path", lambda: path)
    return path


def test_default_config_values(isolated_config_path: Path) -> None:
    cfg = config.UserConfig()
    assert cfg.mode == "work"
    assert cfg.theme == "web_dark"
    assert cfg.minimize_to_tray is True
    assert cfg.check_updates is True
    assert cfg.readability_protection is True


def test_roundtrip_to_dict_from_dict() -> None:
    cfg = config.UserConfig(
        mode="code",
        theme="web_light",
        workspace=r"C:\work\demo",
        last_model="deepseek-chat",
        panel_ratios={"left": 0.2, "center": 0.58, "right": 0.22},
    )
    restored = config.UserConfig.from_dict(cfg.to_dict())
    assert restored == cfg


def test_from_dict_ignores_unknown_keys() -> None:
    cfg = config.UserConfig.from_dict({"mode": "code", "nonexistent_key": 42})
    assert cfg.mode == "code"
    assert not hasattr(cfg, "nonexistent_key")


def test_load_missing_creates_default_file(isolated_config_path: Path) -> None:
    cfg = config.UserConfig.load()
    assert cfg.theme == config.C.DEFAULT_THEME
    # load 缺文件时应自动落盘，后续再读稳定
    assert isolated_config_path.exists()


def test_load_corrupt_json_falls_back_to_default(
    isolated_config_path: Path,
) -> None:
    isolated_config_path.write_text("{not valid json", encoding="utf-8")
    cfg = config.UserConfig.load()
    assert cfg.mode == "work"
    assert cfg.theme == "web_dark"


def test_legacy_theme_migration(isolated_config_path: Path) -> None:
    """旧版主题名（midnight_ocean 等）加载后自动迁移到 Web 主题。"""
    legacy = {
        "midnight_ocean": "web_dark",
        "forest_green": "web_dark",
        "qinghua": "web_dark",
        "daylight": "web_light",
    }
    for old, expected in legacy.items():
        isolated_config_path.write_text(
            json.dumps({"theme": old}), encoding="utf-8"
        )
        cfg = config.UserConfig.load()
        assert cfg.theme == expected, f"{old} 应迁移为 {expected}"


def test_save_roundtrip_through_disk(isolated_config_path: Path) -> None:
    cfg = config.UserConfig(mode="code", theme="web_light", workspace="C:/x")
    cfg.save()
    data = json.loads(isolated_config_path.read_text(encoding="utf-8"))
    assert data["mode"] == "code"
    assert data["theme"] == "web_light"
    assert data["workspace"] == "C:/x"


def test_get_dsh_base_url_default() -> None:
    """未配置自定义端点时返回默认 loopback base URL。"""
    assert config.get_dsh_base_url() == config.C.DSH_BASE_URL


@pytest.mark.parametrize(
    ("endpoint", "expected"),
    [
        ("http://127.0.0.1:3080", "ws://127.0.0.1:3080"),
        ("https://dsh.example.com", "wss://dsh.example.com"),
        ("http://192.168.1.5:9999/", "ws://192.168.1.5:9999"),
    ],
)
def test_get_dsh_ws_url_custom_endpoint(
    isolated_config_path: Path, endpoint: str, expected: str
) -> None:
    cfg = config.UserConfig(custom_dsh_endpoint=endpoint)
    cfg.save()
    assert config.get_dsh_ws_url() == expected


def test_get_dsh_ws_url_https_maps_to_wss(isolated_config_path: Path) -> None:
    cfg = config.UserConfig(custom_dsh_endpoint="https://dsh.example.com")
    cfg.save()
    assert config.get_dsh_ws_url().startswith("wss://")


def test_get_dsh_home_dir_uses_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DSH_HOME", r"C:\custom\dsh")
    assert str(config.get_dsh_home_dir()) == r"C:\custom\dsh"


def test_get_dsh_home_dir_default() -> None:
    assert config.get_dsh_home_dir().name == ".dsh"
