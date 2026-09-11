"""剪贴板猫娘（neko_clipboard_watcher）v0.1 · 作者：MENGYAOYUE

监听剪贴板变化，猫娘看到主人复制了什么就主动搭话：
链接 → 要不要看看；代码 → 凑过来看故事；英文 → 要不要翻译；长文 → 要不要总结。

设计要点：
- 感知类功能独立成插件，与定时关怀类（neko_daily_fortune）分离
- 纯标准库 ctypes 读剪贴板（不弹窗、不依赖第三方库）
- 隐私优先：疑似密码/密钥/令牌的内容**看都不看**；全部处理在本地完成
- 节奏控制：去重 + 冷却 + 每小时上限 + 总开关，绝不做话痨猫
- 推送走 ctx.push_message（ai_behavior=respond），猫娘用自己的人设开口
"""

from __future__ import annotations

import ctypes
import json
import threading
from pathlib import Path
from typing import Any, Optional

from plugin.sdk.plugin import (
    Err,
    NekoPluginBase,
    Ok,
    SdkError,
    lifecycle,
    llm_tool,
    neko_plugin,
    plugin_entry,
)

from ._clipboard_logic import (
    CommentGate,
    build_comment,
    classify_content,
)

_PLUGIN_ID = "neko_clipboard_watcher"

_DEFAULTS: dict[str, Any] = {
    "poll_interval_seconds": 2.0,
    "cooldown_seconds": 600.0,
    "max_per_hour": 6,
    "push_enabled": True,
}


def _safe_float(value: Any, default: float) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return default
    return default


def _safe_int(value: Any, default: int) -> int:
    return int(_safe_float(value, default))


def _safe_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in {"1", "true", "yes", "on", "开"}:
            return True
        if low in {"0", "false", "no", "off", "关"}:
            return False
    return default


def read_clipboard_text() -> Optional[str]:
    """用 ctypes 读取剪贴板纯文本（CF_UNICODETEXT）。

    返回 ``None`` 表示本次读不到（别的程序占用剪贴板等），空串表示剪贴板是空的。
    Windows 专属；非 Windows 返回 ``None``。
    """
    if not hasattr(ctypes, "windll"):
        return None
    CF_UNICODETEXT = 13
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    if not user32.OpenClipboard(None):
        return None
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            return ""
        try:
            return ctypes.wstring_at(ptr)
        finally:
            kernel32.GlobalUnlock(handle)
    except Exception:
        return None
    finally:
        user32.CloseClipboard()


@neko_plugin
class ClipboardWatcherPlugin(NekoPluginBase):
    """剪贴板猫娘：感知主人复制的内容并主动搭话（节奏可控）。"""

    def __init__(self, ctx):
        super().__init__(ctx)
        self.file_logger = self.enable_file_logging(log_level="INFO")
        self.logger = self.file_logger

        self.data_dir = Path(self.data_path())
        self.state_path = self.data_dir / "clipboard_state.json"

        self.poll_interval_seconds: float = _DEFAULTS["poll_interval_seconds"]
        self.cooldown_seconds: float = _DEFAULTS["cooldown_seconds"]
        self.max_per_hour: int = _DEFAULTS["max_per_hour"]
        self.gate = CommentGate(
            cooldown_seconds=self.cooldown_seconds,
            max_per_hour=self.max_per_hour,
        )
        self._config_loaded = False

        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None

    # ── 配置与状态 ─────────────────────────────────────────────
    async def _load_config(self) -> None:
        try:
            cfg = await self.config.dump(timeout=5.0)
        except Exception as exc:
            self.logger.warning("[clipboard] 读取配置失败：{}", exc)
            cfg = {}
        section = cfg.get(_PLUGIN_ID) if isinstance(cfg, dict) else None
        section = section if isinstance(section, dict) else {}

        self.poll_interval_seconds = max(1.0, _safe_float(
            section.get("poll_interval_seconds"), _DEFAULTS["poll_interval_seconds"]
        ))
        self.cooldown_seconds = max(10.0, _safe_float(
            section.get("cooldown_seconds"), _DEFAULTS["cooldown_seconds"]
        ))
        self.max_per_hour = max(1, _safe_int(section.get("max_per_hour"), _DEFAULTS["max_per_hour"]))
        self.gate = CommentGate(
            cooldown_seconds=self.cooldown_seconds,
            max_per_hour=self.max_per_hour,
        )
        self._load_state()
        self._config_loaded = True

    async def _ensure_config_loaded(self) -> None:
        if not self._config_loaded:
            await self._load_config()

    def _load_state(self) -> None:
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            state = None
        if isinstance(state, dict):
            self.gate.load_state(state.get("gate"))

    def _save_state(self) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps({"gate": self.gate.to_state()}, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as exc:
            self.logger.warning("[clipboard] 状态写入失败：{}", exc)

    # ── 生命周期 ───────────────────────────────────────────────
    @lifecycle(id="startup")
    async def startup(self, **_):
        await self._load_config()
        self._stop_event.clear()
        self._wake_event.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="neko-clipboard-poll"
        )
        self._poll_thread.start()
        self.logger.info(
            "[clipboard] 启动：enabled={}, interval={}s, cooldown={}s, max/h={}",
            self.gate.enabled, self.poll_interval_seconds,
            self.cooldown_seconds, self.max_per_hour,
        )
        return Ok({"status": "running", "version": "0.1.0"})

    @lifecycle(id="shutdown")
    def shutdown(self, **_):
        self._stop_event.set()
        self._wake_event.set()
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=3.0)
        self._save_state()
        self.logger.info("[clipboard] 关闭")
        return Ok("stopped")

    # ── 轮询线程 ───────────────────────────────────────────────
    def _poll_loop(self) -> None:
        last_text: Optional[str] = None
        while not self._stop_event.is_set():
            try:
                text = read_clipboard_text()
                if text is not None and text != last_text:
                    last_text = text
                    self._on_clipboard_changed(text)
            except Exception:
                self.logger.exception("[clipboard] 轮询异常")
            self._wake_event.clear()
            if self._stop_event.is_set():
                break
            self._wake_event.wait(timeout=max(1.0, self.poll_interval_seconds))
        self.logger.info("[clipboard] 轮询线程退出")

    def _on_clipboard_changed(self, text: str) -> None:
        allowed, reason = self.gate.allow(text)
        if not allowed:
            self.logger.info("[clipboard] 跳过评论：{}（长度 {}）", reason, len(text or ""))
            return
        info = classify_content(text)
        comment = build_comment(info["kind"], info["snippet"])
        if not comment:
            return
        self.gate.record(text)
        self._save_state()
        self._push(comment)

    def _push(self, text: str) -> None:
        try:
            self.ctx.push_message(
                source=_PLUGIN_ID,
                visibility=[],
                ai_behavior="respond",
                parts=[{"type": "text", "text": text}],
                priority=3,
                metadata={"description": "🐱 剪贴板猫娘"},
            )
        except Exception:
            self.logger.exception("[clipboard] push_message 失败")

    # ── 功能入口 ───────────────────────────────────────────────
    @llm_tool(
        name="neko_clipboard_now",
        description="读取主人当前剪贴板里的内容并分类（链接/代码/英文/长文等），猫娘据此搭话或帮忙。疑似密码/密钥的内容会被拒绝读取。",
        parameters={"type": "object", "properties": {}},
        timeout=10.0,
    )
    @plugin_entry(
        id="clipboard_now",
        name="看看剪贴板",
        description="读取当前剪贴板内容并分类，返回猫娘口吻的搭话文本；敏感内容拒绝读取。",
        input_schema={"type": "object", "properties": {}},
    )
    async def clipboard_now_entry(self, **_):
        await self._ensure_config_loaded()
        text = read_clipboard_text()
        if text is None:
            return Err(SdkError("喵呜…本喵读不到剪贴板（可能被别的程序占用了）。"))
        cleaned = (text or "").strip()
        if not cleaned:
            return Ok("剪贴板空空如也喵～主人复制点什么本喵再来看？")
        info = classify_content(cleaned)
        if info["kind"] == "sensitive":
            return Ok("这里面的内容看起来是密码或密钥喵，本喵就装作没看见，主人也要保管好哦！")
        comment = build_comment(info["kind"], info["snippet"]) or "主人复制了新东西喵！"
        return Ok(comment)

    @plugin_entry(
        id="set_enabled",
        name="剪贴板监听开关",
        description="开启/关闭剪贴板监听：enabled=true/false。",
        input_schema={
            "type": "object",
            "properties": {"enabled": {"type": "boolean"}},
            "required": ["enabled"],
        },
    )
    async def set_enabled_entry(self, enabled: bool = True, **_):
        await self._ensure_config_loaded()
        self.gate.enabled = bool(enabled)
        self._save_state()
        return Ok(f"剪贴板监听已{'开启' if enabled else '关闭'}喵～")

    @plugin_entry(
        id="status",
        name="剪贴板监听状态",
        description="查看监听开关、冷却与配额状态。",
        input_schema={"type": "object", "properties": {}},
    )
    async def status_entry(self, **_):
        await self._ensure_config_loaded()
        return Ok(
            "🐱 剪贴板猫娘：\n"
            f"- 监听：{'✅ 开' if self.gate.enabled else '⛔ 关'}\n"
            f"- 轮询间隔：{self.poll_interval_seconds} 秒｜冷却：{int(self.cooldown_seconds)} 秒\n"
            f"- 本小时已搭话：{len(self.gate.push_times)} / {self.max_per_hour} 次"
        )
