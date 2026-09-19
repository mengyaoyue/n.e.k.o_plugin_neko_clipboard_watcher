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
