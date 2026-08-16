"""外置视觉模型工具：inspect_image。

把本地文件路径或 URL 图片发送到任意 OpenAI 兼容视觉端点，返回模型描述文本。

支持的端点格式：OpenAI Chat Completions（message content 为多模态数组）
  POST {api_base}/chat/completions
  messages: [
    {
      "role": "user",
      "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,...."}}
      ]
    }
  ]

兼容范围：
  - 通义千问 VL（dashscope 兼容模式）
  - 智谱 GLM-4V（open.bigmodel.cn）
  - Ollama（本地 llava / bakllava 等多模态模型，/v1 兼容前缀）
  - 其他任何实现了 OpenAI Chat Completions 多模态接口的服务

图片处理：
  - 本地文件：按 vision_max_image_size 做客户端侧等比下采样 → JPEG → base64
  - URL：优先让模型侧自行 fetch（端点不支持时，客户端下载后按本地流程处理）
"""

from __future__ import annotations

import base64
import io
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter

from .. import constants as C
from ..config import UserConfig
from ..utils.logger import get_logger

log = get_logger("tools.inspect_image")

# 工具名（必须与 Agent 请求调用的工具名一致）
TOOL_NAME = "inspect_image"

# 支持的图片扩展名（本地文件白名单）
SUPPORTED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff"}

# MIME 映射（用于 data: URL）
EXT_TO_MIME = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tiff": "image/tiff",
}


@dataclass
class InspectImageResult:
    """inspect_image 调用结果。"""
    ok: bool
    description: str = ""          # 模型输出的描述文本
    error: str = ""                # 错误信息（ok=False 时）
    image_source: str = ""         # 图片来源标记（local / url / url-fetched-local）
    model: str = ""                # 实际使用的视觉模型
    elapsed_ms: int = 0            # 耗时（毫秒）
    tokens_in: int = 0             # 输入 token 估算（部分端点不返回）
    tokens_out: int = 0            # 输出 token 估算

    @property
    def summary(self) -> str:
        """给 Agent 看的简洁摘要（成功返回描述，失败返回带原因的错误文本）。"""
        if self.ok:
            return self.description or "(空描述)"
        return f"[inspect_image 失败] {self.error or '未知错误'}"


class InspectImageTool:
    """inspect_image 工具（可重用实例，内部维护独立 HTTP 连接池）。"""

    def __init__(self, config: UserConfig | None = None):
        self._config_ref = config  # 可以是 None，调用时再读取（支持设置热更新）
        self._session = requests.Session()
        adapter = HTTPAdapter(
            pool_connections=C.HTTP_POOL_CONNECTIONS,
            pool_maxsize=C.HTTP_POOL_MAXSIZE,
        )
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

    # ===== 配置读取 =====

    def _get_config(self) -> UserConfig:
        if self._config_ref is not None:
            return self._config_ref
        return UserConfig.load()

    # ===== 图片加载 =====

    @staticmethod
    def is_url(image: str) -> bool:
        """判断输入是本地路径还是 URL（http/https/data）。"""
        if not image:
            return False
        p = urlparse(image)
        return p.scheme in {"http", "https", "data"}

    @staticmethod
    def _downsample_image(data: bytes, max_size: int, mime: str) -> tuple[bytes, str]:
        """客户端侧下采样：长边不超过 max_size 像素，输出 JPEG。

        返回：(out_bytes, out_mime)。如果 PIL 不可用，原样返回。
        """
        if max_size <= 0:
            return data, mime
        try:
            from PIL import Image  # 懒加载：没装 Pillow 也不影响其他功能
        except ImportError:
            log.debug("Pillow 未安装，跳过客户端下采样")
            return data, mime
        try:
            im = Image.open(io.BytesIO(data))
            im.load()
            w, h = im.size
            long_side = max(w, h)
            if long_side <= max_size:
                return data, mime
            scale = max_size / long_side
            new_w, new_h = max(1, int(w * scale)), max(1, int(h * scale))
            resized = im.resize((new_w, new_h), Image.LANCZOS)
            # RGB 化：RGBA/P/LA 转 RGB，保存为 JPEG（兼容性最好）
            if resized.mode not in ("RGB", "L"):
                resized = resized.convert("RGB")
            buf = io.BytesIO()
            resized.save(buf, format="JPEG", quality=85, optimize=True)
            log.info("图片已下采样 %dx%d → %dx%d", w, h, new_w, new_h)
            return buf.getvalue(), "image/jpeg"
        except Exception as e:
            log.warning("图片下采样失败，使用原图: %s", e)
            return data, mime

    @staticmethod
    def _load_local(path: str, max_size: int) -> tuple[str, str]:
        """加载本地图片 → data: URL。返回 (data_url, source_label)。"""
        p = Path(path).expanduser()
        if not p.exists():
            raise FileNotFoundError(f"本地图片不存在: {path}")
        if not p.is_file():
            raise ValueError(f"不是文件: {path}")
        ext = p.suffix.lower()
        if ext not in SUPPORTED_EXT:
            raise ValueError(f"不支持的图片格式: {ext}（支持: {', '.join(sorted(SUPPORTED_EXT))}）")
        raw = p.read_bytes()
        mime = EXT_TO_MIME.get(ext, "image/jpeg")
        out_bytes, out_mime = InspectImageTool._downsample_image(raw, max_size, mime)
        b64 = base64.b64encode(out_bytes).decode("ascii")
        return f"data:{out_mime};base64,{b64}", "local"

    @staticmethod
    def _load_url(url: str, max_size: int, session: requests.Session,
                  prefer_direct: bool = True) -> tuple[str, str]:
        """加载 URL 图片。

        Args:
            prefer_direct: True 时优先让模型自己 fetch（直接返回原 URL），
              False 或端点不支持远程 URL 时，客户端下载后 base64 打包返回。
              目前我们默认直接返回 URL，大多数云服务商视觉端点都支持；
              如果端点返回错误（如"Invalid image URL"），调用方可重试时
              传 prefer_direct=False 走客户端下载路径。
        """
        if prefer_direct:
            return url, "url"
        # 客户端下载后打包
        try:
            resp = session.get(url, timeout=C.HTTP_TIMEOUT_SEC)
            resp.raise_for_status()
        except requests.RequestException as e:
            raise RuntimeError(f"下载 URL 图片失败: {e}") from e
        data = resp.content
        # 从 Content-Type 或 URL 后缀判断 mime
        mime = resp.headers.get("Content-Type", "").split(";")[0].strip()
        if not mime or not mime.startswith("image/"):
            ext = Path(urlparse(url).path).suffix.lower()
            mime = EXT_TO_MIME.get(ext, "image/jpeg")
        out_bytes, out_mime = InspectImageTool._downsample_image(data, max_size, mime)
        b64 = base64.b64encode(out_bytes).decode("ascii")
        return f"data:{out_mime};base64,{b64}", "url-fetched-local"

    # ===== 主调用 =====

    def call(self, image: str, prompt: str = "", detail: str = "auto") -> InspectImageResult:
        """调用视觉模型。

        Args:
            image: 本地文件路径或 http(s):// URL 或 data: URL
            prompt: 提问文本（留空则使用 config.vision_default_prompt）
            detail: OpenAI detail 参数（low/high/auto），多数自定义端点可忽略

        Returns:
            InspectImageResult
        """
        t0 = time.time()
        cfg = self._get_config()
        api_base = (cfg.vision_api_base or "").rstrip("/")
        api_key = cfg.vision_api_key or ""
        model = cfg.vision_model or ""
        max_size = int(getattr(cfg, "vision_max_image_size", 1024) or 1024)
        default_prompt = getattr(cfg, "vision_default_prompt", "") or ""

        # ---- 配置校验 ----
        if not api_base:
            return InspectImageResult(
                ok=False,
                error="视觉端点未配置：请在 设置 → 外置视觉模型 中填入 API Base 和模型名。"
                      "示例：Ollama 本地 → http://127.0.0.1:11434/v1 , 模型 llava:7b",
                elapsed_ms=int((time.time() - t0) * 1000),
            )
        if not model:
            return InspectImageResult(
                ok=False,
                error="视觉模型名未配置：请在 设置 → 外置视觉模型 中选择/填入模型名。",
                elapsed_ms=int((time.time() - t0) * 1000),
            )

        # ---- 图片加载 ----
        try:
            if self.is_url(image):
                img_data, source = self._load_url(image, max_size, self._session, prefer_direct=True)
            else:
                img_data, source = self._load_local(image, max_size)
        except Exception as e:
            log.warning("图片加载失败: %s", e)
            return InspectImageResult(
                ok=False,
                error=f"图片加载失败: {e}",
                elapsed_ms=int((time.time() - t0) * 1000),
            )

        # ---- 组装请求 ----
        text_prompt = prompt.strip() or default_prompt or "请描述这张图片。"
        endpoint = f"{api_base}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": text_prompt},
                        {"type": "image_url", "image_url": {"url": img_data, "detail": detail}},
                    ],
                }
            ],
            "temperature": 0.2,
            "max_tokens": 1024,
        }

        try:
            resp = self._session.post(
                endpoint, headers=headers, json=payload, timeout=90,
            )
            # 如果 URL 图片远端返回错误，可能是端点不支持 fetch，尝试回退到客户端下载后重试
            if (resp.status_code >= 400 and source == "url" and not image.startswith("data:")):
                log.info("URL 图片直传给端点失败（HTTP %d），尝试客户端下载后重试", resp.status_code)
                try:
                    img_data, source = self._load_url(image, max_size, self._session, prefer_direct=False)
                except Exception as fe:
                    log.warning("回退下载也失败: %s", fe)
                else:
                    payload["messages"][0]["content"][1]["image_url"]["url"] = img_data
                    resp = self._session.post(
                        endpoint, headers=headers, json=payload, timeout=90,
                    )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as e:
            log.error("视觉端点请求失败: %s", e)
            return InspectImageResult(
                ok=False,
                error=f"视觉端点请求失败: {e}",
                image_source=source,
                model=model,
                elapsed_ms=int((time.time() - t0) * 1000),
            )
        except ValueError as e:
            log.error("视觉端点返回非 JSON: %s", e)
            return InspectImageResult(
                ok=False,
                error=f"视觉端点返回格式错误: {e}",
                image_source=source,
                model=model,
                elapsed_ms=int((time.time() - t0) * 1000),
            )

        # ---- 解析响应（OpenAI Chat Completions 标准格式）----
        try:
            choices = data.get("choices") or []
            if not choices:
                return InspectImageResult(
                    ok=False,
                    error=f"视觉端点返回空 choices: {data}",
                    image_source=source,
                    model=model,
                    elapsed_ms=int((time.time() - t0) * 1000),
                )
            msg = choices[0].get("message") or {}
            content = msg.get("content") or ""
            # 有些端点 content 可能是 list（多模态回传），取其中 text 段
            if isinstance(content, list):
                parts: list[str] = []
                for seg in content:
                    if isinstance(seg, dict) and seg.get("type") == "text":
                        parts.append(str(seg.get("text", "")))
                    elif isinstance(seg, str):
                        parts.append(seg)
                content = "\n".join(p for p in parts if p).strip()

            usage = data.get("usage") or {}
            tokens_in = int(usage.get("prompt_tokens", 0))
            tokens_out = int(usage.get("completion_tokens", 0))

            return InspectImageResult(
                ok=True,
                description=str(content),
                image_source=source,
                model=model,
                elapsed_ms=int((time.time() - t0) * 1000),
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            )
        except Exception as e:
            log.error("解析视觉响应失败: %s, data=%s", e, data)
            return InspectImageResult(
                ok=False,
                error=f"解析视觉响应失败: {e}",
                image_source=source,
                model=model,
                elapsed_ms=int((time.time() - t0) * 1000),
            )

    def call_async(self, callback: Callable[[InspectImageResult], None],
                   image: str, prompt: str = "", detail: str = "auto") -> None:
        """异步调用（后台线程，结果通过回调返回）。"""
        def _worker():
            try:
                result = self.call(image=image, prompt=prompt, detail=detail)
            except Exception as e:
                log.error("inspect_image 异步调用异常: %s", e, exc_info=True)
                result = InspectImageResult(ok=False, error=f"未预期异常: {e}")
            try:
                callback(result)
            except Exception as e:
                log.error("inspect_image 回调异常: %s", e)

        t = threading.Thread(target=_worker, daemon=True, name="inspect-image")
        t.start()

    def close(self) -> None:
        self._session.close()


# ===== 便捷函数（懒加载单例） =====

_default_instance: InspectImageTool | None = None
_instance_lock = threading.Lock()


def _get_default() -> InspectImageTool:
    global _default_instance
    if _default_instance is None:
        with _instance_lock:
            if _default_instance is None:
                _default_instance = InspectImageTool()
    return _default_instance


def inspect_image_sync(image: str, prompt: str = "",
                       detail: str = "auto") -> InspectImageResult:
    """便捷函数：同步调用 inspect_image。"""
    return _get_default().call(image=image, prompt=prompt, detail=detail)


def inspect_image_async(callback: Callable[[InspectImageResult], None],
                        image: str, prompt: str = "",
                        detail: str = "auto") -> None:
    """便捷函数：异步调用 inspect_image。"""
    _get_default().call_async(callback, image=image, prompt=prompt, detail=detail)
