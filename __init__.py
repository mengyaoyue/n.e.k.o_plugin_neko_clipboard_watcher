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
from ._panel import PanelServer, find_open_port, guess_mime

_PLUGIN_ID = "neko_clipboard_watcher"

_DEFAULTS: dict[str, Any] = {
    "poll_interval_seconds": 2.0,
    "cooldown_seconds": 5.0,
    "max_per_hour": 30,
    "push_enabled": True,
}


def _safe_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


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
    # 64 位下必须显式声明类型，否则句柄被截断成 32 位、解引用时越界崩溃
    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.restype = ctypes.c_bool
    user32.GetClipboardData.argtypes = [ctypes.c_uint]
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.CloseClipboard.restype = ctypes.c_bool
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = ctypes.c_bool
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
        self._panel_server = None
        self._panel_port: int = 15700

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
        self.cooldown_seconds = max(2.0, _safe_float(
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
            state = self._read_state_dict()
            state["gate"] = self.gate.to_state()
            self.state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
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
        self._start_panel()
        self.logger.info(
            "[clipboard] 启动：enabled={}, interval={}s, cooldown={}s, max/h={}",
            self.gate.enabled, self.poll_interval_seconds,
            self.cooldown_seconds, self.max_per_hour,
        )
        return Ok({"status": "running", "version": "0.1.0"})

    def _start_panel(self) -> None:
        endpoints = {
            ("GET", "/api/status"): self._panel_status,
            ("POST", "/api/toggle"): self._panel_toggle,
            ("POST", "/api/config"): self._panel_config,
            ("GET", "/api/panel_prefs"): self._panel_prefs,
            ("POST", "/api/panel_prefs"): self._panel_prefs,
            ("GET", "/api/background"): self._panel_background,
            ("POST", "/api/background"): self._panel_background,
        }
        port = find_open_port(self._panel_port)
        server = PanelServer(
            port,
            self._panel_html,
            endpoints,
            static_dir=Path(__file__).parent / "static",
            asset_provider=self._bg_asset_for,
        )
        if server.start():
            self._panel_server = server
            self._panel_port = port
            self.logger.info("[clipboard] 管理面板已启动: http://127.0.0.1:{}", port)
            try:
                registered = self.register_static_ui("static")
                self.logger.info("[clipboard] static UI 注册: {}", registered)
            except Exception as exc:
                self.logger.warning("[clipboard] static UI 注册失败: {}", exc)
        else:
            self.logger.warning("[clipboard] 管理面板启动失败")

    def _panel_status(self, _body: dict[str, Any]) -> dict[str, Any]:
        return {
            "enabled": self.gate.enabled,
            "poll_interval": self.poll_interval_seconds,
            "cooldown_seconds": int(self.gate.cooldown_seconds),
            "max_per_hour": self.gate.max_per_hour,
            "pushed_this_hour": len(self.gate.push_times),
            "tracked": len(self.gate.recent_hashes),
            "panel_port": self._panel_port,
            "prefs": self._ui_state(),
            "background": self._background_state(),
        }

    def _panel_toggle(self, body: dict[str, Any]) -> dict[str, Any]:
        self.gate.enabled = bool(body.get("enabled"))
        self._save_state()
        return {"ok": True, "enabled": self.gate.enabled}

    # ── 界面偏好与自定义背景（图存 data/，不进安装包）──────────────
    _PANEL_PREFS_DEFAULT = {
        "ui_font": "system",
        "ui_font_size": "m",
        "ui_trail": "on",
        "bg_mode": "default",
        "bg_dim": "medium",
    }
    _BG_MAX_BYTES = 8 * 1024 * 1024
    _BG_MIME_EXT = {
        "image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
        "image/webp": ".webp", "image/gif": ".gif",
    }

    def _read_state_dict(self) -> dict:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _ui_state(self) -> dict:
        state = self._read_state_dict()
        ui = state.get("ui")
        ui = ui if isinstance(ui, dict) else {}
        merged = dict(self._PANEL_PREFS_DEFAULT)
        for key in self._PANEL_PREFS_DEFAULT:
            value = ui.get(key)
            if isinstance(value, str) and value.strip():
                merged[key] = value.strip()[:32]
        return merged

    def _save_ui_state(self, ui: dict) -> None:
        """只改 ui 段，保留 gate（否则会把监听状态冲掉）。"""
        state = self._read_state_dict()
        state["gate"] = self.gate.to_state()
        state["ui"] = ui
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            self.logger.warning("[clipboard] 界面偏好写入失败：{}", exc)

    def _panel_prefs(self, body: dict) -> dict:
        prefs = self._ui_state()
        payload = body if isinstance(body, dict) else {}
        changed = False
        for key in ("ui_font", "ui_font_size", "ui_trail", "bg_mode", "bg_dim"):
            value = _safe_str(payload.get(key)).strip()
            if value and value != prefs[key]:
                prefs[key] = value[:32]
                changed = True
        if changed:
            self._save_ui_state(prefs)
        return {"ok": True, "prefs": prefs}

    def _bg_dir(self) -> Path:
        path = self.state_path.parent / "backgrounds"
        try:
            path.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return path

    def _bg_file(self) -> Optional[Path]:
        name = _safe_str(self._ui_state().get("bg_file")).strip() if hasattr(self, "_ui_state") else ""
        if name:
            candidate = (self._bg_dir() / Path(name).name).resolve()
            if candidate.is_file():
                return candidate
        for suffix in (".png", ".jpg", ".webp", ".gif"):
            candidate = self._bg_dir() / f"custom{suffix}"
            if candidate.is_file():
                return candidate
        return None

    def _background_state(self) -> dict:
        ui = self._ui_state()
        mode = ui.get("bg_mode") or "default"
        if mode not in ("default", "custom", "plain"):
            mode = "default"
        custom = self._bg_file()
        if mode == "custom" and custom is None:
            mode = "default"          # 图没了就退回默认，别留一片空白
        return {
            "mode": mode,
            "dim": ui.get("bg_dim") or "medium",
            "has_custom": custom is not None,
            "custom_bytes": custom.stat().st_size if custom is not None else 0,
            "custom_name": custom.name if custom is not None else "",
            "custom_path": "/bg/custom",
        }

    def _bg_asset(self) -> Optional[tuple[bytes, str]]:
        path = self._bg_file()
        if path is None:
            return None
        try:
            return path.read_bytes(), guess_mime(path.name)
        except Exception:
            return None

    def _bg_asset_for(self, rel: str) -> Optional[tuple[bytes, str]]:
        rel = (rel or "").strip().lstrip("/").split("?", 1)[0]
        if rel in ("bg/custom", "bg/custom.jpg", "bg/custom.png"):
            return self._bg_asset()
        return None

    def _panel_background(self, body: dict) -> dict:
        import base64
        payload = body if isinstance(body, dict) else {}
        action = _safe_str(payload.get("action")).strip() or "mode"
        ui = self._ui_state()
        if action == "upload":
            raw = _safe_str(payload.get("image_base64") or payload.get("image")).strip()
            if not raw:
                return {"ok": False, "error": "没有收到图片数据。", "background": self._background_state()}
            if raw.startswith("data:"):
                head, _, encoded = raw.partition(",")
                mime = head[5:].split(";")[0].strip().lower()
            else:
                encoded, mime = raw, "image/png"
            ext = self._BG_MIME_EXT.get(mime)
            if not ext:
                return {"ok": False, "error": f"不支持的格式：{mime or '未知'}", "background": self._background_state()}
            try:
                blob = base64.b64decode(encoded or "", validate=False)
            except Exception:
                return {"ok": False, "error": "图片数据解不开。", "background": self._background_state()}
            if not blob:
                return {"ok": False, "error": "图片是空的。", "background": self._background_state()}
            if len(blob) > self._BG_MAX_BYTES:
                return {"ok": False, "error": f"图片超过 {self._BG_MAX_BYTES // 1048576}MB 限制。", "background": self._background_state()}
            for suffix in (".png", ".jpg", ".webp", ".gif"):
                stale = self._bg_dir() / f"custom{suffix}"
                if stale.is_file():
                    try:
                        stale.unlink()
                    except Exception:
                        pass
            target = self._bg_dir() / f"custom{ext}"
            try:
                target.write_bytes(blob)
            except Exception as exc:
                return {"ok": False, "error": str(exc), "background": self._background_state()}
            ui["bg_mode"] = "custom"
            ui["bg_file"] = target.name
            self._save_ui_state(ui)
            return {"ok": True, "message": "背景已换成你上传的图。", "background": self._background_state()}
        if action == "reset":
            ui["bg_mode"] = "default"
            self._save_ui_state(ui)
            return {"ok": True, "message": "已恢复默认背景。", "background": self._background_state()}
        mode = _safe_str(payload.get("mode")).strip() or ui.get("bg_mode") or "default"
        if mode not in ("default", "custom", "plain"):
            return {"ok": False, "error": f"未知模式：{mode}", "background": self._background_state()}
        if mode == "custom" and self._bg_file() is None:
            return {"ok": False, "error": "还没上传过背景图。", "background": self._background_state()}
        ui["bg_mode"] = mode
        dim = _safe_str(payload.get("dim")).strip()
        if dim in ("light", "medium", "strong"):
            ui["bg_dim"] = dim
        self._save_ui_state(ui)
        return {"ok": True, "background": self._background_state()}

    def _panel_config(self, body: dict[str, Any]) -> dict[str, Any]:
        if "cooldown_seconds" in body:
            self.gate.cooldown_seconds = max(10.0, float(body["cooldown_seconds"]))
            self.cooldown_seconds = self.gate.cooldown_seconds
        if "max_per_hour" in body:
            self.gate.max_per_hour = max(1, int(body["max_per_hour"]))
        if "poll_interval" in body:
            self.poll_interval_seconds = max(1.0, float(body["poll_interval"]))
        self._save_state()
        return self._panel_status({})

    def _panel_html(self) -> str:
        page = Path(__file__).parent / "static" / "index.html"
        try:
            return page.read_text(encoding="utf-8")
        except Exception:
            return "<h1>面板页缺失喵（static/index.html）</h1>"

    @lifecycle(id="shutdown")
    def shutdown(self, **_):
        if self._panel_server:
            self._panel_server.stop()
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
