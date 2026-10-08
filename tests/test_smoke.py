"""剪贴板猫娘：结构契约 + 核心逻辑冒烟（pytest 风格，不读真实剪贴板）"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_logic_module():
    spec = importlib.util.spec_from_file_location(
        "neko_clipboard_watcher_logic", ROOT / "_clipboard_logic.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["neko_clipboard_watcher_logic"] = mod
    spec.loader.exec_module(mod)
    return mod


class TestPluginManifest:
    def test_plugin_toml_exists(self):
        assert (ROOT / "plugin.toml").is_file()

    def test_entry_declared(self):
        text = (ROOT / "plugin.toml").read_text(encoding="utf-8")
        assert 'id = "neko_clipboard_watcher"' in text
        assert "VoiceInputPlugin" not in text  # 防串插件


class TestClassify:
    def test_classify_url(self):
        mod = _load_logic_module()
        assert mod.classify_content("https://github.com/x")["kind"] == "url"

    def test_classify_sensitive(self):
        mod = _load_logic_module()
        assert mod.classify_content("password=hunter2")["kind"] == "sensitive"

    def test_classify_ignore_short(self):
        mod = _load_logic_module()
        assert mod.classify_content("嗯")["kind"] == "ignore"

    def test_build_comment_none_for_sensitive(self):
        mod = _load_logic_module()
        assert mod.build_comment("sensitive", "x") is None


class TestCommentGate:
    def test_dedup(self):
        mod = _load_logic_module()
        gate = mod.CommentGate(cooldown_seconds=10, max_per_hour=6, now_fn=lambda: 1000.0)
        assert gate.allow("今天也要元气满满喵")[0] is True
        gate.record("今天也要元气满满喵")
        assert gate.allow("今天也要元气满满喵")[0] is False

    def test_sensitive_blocked(self):
        mod = _load_logic_module()
        gate = mod.CommentGate(cooldown_seconds=10, max_per_hour=6, now_fn=lambda: 1000.0)
        allowed, reason = gate.allow("password=abc123")
        assert allowed is False and "sensitive" in reason

    def test_switch(self):
        mod = _load_logic_module()
        gate = mod.CommentGate(cooldown_seconds=10, max_per_hour=6, now_fn=lambda: 1000.0)
        gate.enabled = False
        assert gate.allow("任何内容都可以喵")[0] is False

    def test_sensitive_inside_very_long_text(self):
        """超长文本里藏着密钥也必须是 sensitive——长度分支不许抢在敏感检查前面。"""
        mod = _load_logic_module()
        text = "api_key: sk-abc123def456ghijkl\n" + "喵" * 9000
        info = mod.classify_content(text)
        assert info["kind"] == "sensitive", f"超长文本含密钥应判 sensitive，实际 {info['kind']}"
        gate = mod.CommentGate(cooldown_seconds=0, max_per_hour=99, now_fn=lambda: 1000.0)
        assert gate.allow(text)[0] is False, "gate 也不应放行含密钥的超长文本"

    def test_dedup_survives_long_text(self):
        """同一段超长内容第二次复制要去重：allow 与 record 的哈希口径必须一致。"""
        mod = _load_logic_module()
        gate = mod.CommentGate(cooldown_seconds=0, max_per_hour=99, now_fn=lambda: 1000.0)
        text = "今天也要元气满满喵" + "！" * 9000
        assert gate.allow(text)[0] is True
        gate.record(text)
        allowed, reason = gate.allow(text)
        assert allowed is False and reason == "近期已评论过", f"超长内容应去重，实际 {reason}"


class TestPanelContract:
    """面板结构与限速口径的护栏：防止改回去（冷却下限曾三处不一致：2/2/10）。"""

    def _html(self):
        return (ROOT / "static" / "index.html").read_text(encoding="utf-8")

    def test_cooldown_floor_is_2_everywhere(self):
        html = self._html()
        assert "冷却（秒，≥2）" in html
        assert "≥10" not in html
        assert "Math.max(10" not in html
        assert 'min="2"' in html

    def test_font_size_derived_from_fs(self):
        html = self._html()
        assert "--fs:" in html
        assert "font-size:14px" not in html  # 只允许 --fs 定义处出现基准值

    def test_cached_port_validated(self):
        """findBase 必须先验活缓存端口（端口漂移曾让面板永久假死）。"""
        html = self._html()
        assert "localStorage.removeItem('cb_base')" in html

    def test_no_bottom_hint_texts(self):
        """卡片底部的说明小字按用户要求不保留，防回归。"""
        html = self._html()
        assert "越耗一点 CPU" not in html
        assert "不进安装包" not in html
        assert "超了就不再打扰你" not in html
        assert "蒙层调厚" not in html

    def test_trail_and_click_feedback(self):
        """鼠标轨迹：双事件源兜底 + 点击涟漪反馈。"""
        html = self._html()
        assert "pointermove" in html
        assert "mousemove" in html  # 内嵌 webview 里 pointermove 偶发不派发
        assert "pointerdown" in html  # 点击反馈事件源
        assert "ripples" in html
        assert "RIPPLE_MS" in html

    def test_versions_agree(self):
        toml = (ROOT / "plugin.toml").read_text(encoding="utf-8")
        init = (ROOT / "__init__.py").read_text(encoding="utf-8")
        import re as _re
        m = _re.search(r'version = "([^"]+)"', toml)
        assert m, "plugin.toml 应声明版本"
        version = m.group(1)
        assert f'"""剪贴板猫娘（neko_clipboard_watcher）v{version}' in init
        assert f'"version": "{version}"' in init


class TestOriginGuard:
    """面板来源校验：本机放行、外部网站拒绝。"""

    def _panel(self):
        spec = importlib.util.spec_from_file_location(
            "neko_clipboard_watcher_panel", ROOT / "_panel.py"
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules["neko_clipboard_watcher_panel"] = mod
        spec.loader.exec_module(mod)
        return mod

    def test_local_origins_allowed(self):
        mod = self._panel()
        assert mod.origin_allowed("") is True
        assert mod.origin_allowed("null") is True
        assert mod.origin_allowed("http://127.0.0.1:48916") is True
        assert mod.origin_allowed("http://localhost:15700") is True

    def test_foreign_origins_rejected(self):
        mod = self._panel()
        assert mod.origin_allowed("https://evil.example.com") is False
        assert mod.origin_allowed("http://192.168.1.5:8080") is False
        assert mod.origin_allowed("https://sub.127.0.0.1.nip.io") is False
