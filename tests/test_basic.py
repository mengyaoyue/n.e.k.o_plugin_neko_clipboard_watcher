"""剪贴板猫娘：纯标准库独立测试（python tests/test_basic.py，不读真实剪贴板）"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_logic():
    spec = importlib.util.spec_from_file_location(
        "neko_clipboard_logic", ROOT / "_clipboard_logic.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["neko_clipboard_logic"] = mod
    spec.loader.exec_module(mod)
    return mod


def assert_eq(actual, expected, msg=""):
    if actual != expected:
        raise AssertionError(f"{msg}: expected {expected!r}, got {actual!r}")


def main():
    print("加载 _clipboard_logic ...")
    mod = load_logic()

    # 1. 内容分类
    cases = [
        ("https://github.com/mengyaoyue", "url"),
        ("https://www.bilibili.com/video/BV1xx411c7mD 挺好玩的", "url"),
        ("Traceback (most recent call last):\n  File \"x.py\", line 1", "code"),
        ("def hello():\n    return 'world'", "code"),
        ('{"name": "neko", "age": 3}', "json"),
        ("The quick brown fox jumps over the lazy dog", "english"),
        ("今天也要元气满满喵", "text"),
        ("1,234.56", "number"),
        ("短", "ignore"),
        ("", "ignore"),
    ]
    for text, expected in cases:
        info = mod.classify_content(text)
        assert_eq(info["kind"], expected, f"分类 {text[:20]!r}")

    # 2. 敏感内容：看都不看
    sensitive = [
        "password=hunter2",
        "api_key: sk-abc123def456ghijkl",
        "ghp_0123456789abcdefghijklmnopqrst",
        "-----BEGIN RSA PRIVATE KEY-----",
        "Authorization: Bearer xyz",
    ]
    for text in sensitive:
        info = mod.classify_content(text)
        assert_eq(info["kind"], "sensitive", f"敏感内容应被识别: {text[:20]!r}")
        assert mod.is_sensitive(text), f"is_sensitive 应为真: {text[:20]!r}"
    # 长文
    assert_eq(mod.classify_content("啊" * 400)["kind"], "long", "超长文本应归为 long")

    # 3. 搭话模板：各分类都能生成；敏感/忽略返回 None
    for kind in ("url", "code", "json", "english", "number", "text", "long"):
        comment = mod.build_comment(kind, "测试内容")
        assert comment and "测试内容" in comment, f"{kind} 应生成搭话"
    assert mod.build_comment("sensitive", "x") is None, "敏感不应搭话"
    assert mod.build_comment("ignore", "x") is None, "忽略不应搭话"
    long_comment = mod.build_comment("text", "啊" * 500)
    assert len(long_comment) < 400, f"超长片段应截断: {len(long_comment)}"
    assert "…" in long_comment, "截断应有省略号"

    # 4. CommentGate：去重 / 冷却 / 小时配额 / 开关
    clock = {"t": 1000.0}
    gate = mod.CommentGate(cooldown_seconds=600, max_per_hour=3, now_fn=lambda: clock["t"])
    text = "今天也要元气满满喵"

    allowed, reason = gate.allow(text)
    assert allowed, f"首次应允许: {reason}"
    gate.record(text)

    allowed, reason = gate.allow(text)
    assert not allowed and reason == "近期已评论过", "同内容去重"

    clock["t"] = 1100.0  # 100 秒后，冷却中
    allowed, reason = gate.allow("另一段完全不同的中文内容喵")
    assert not allowed and reason == "冷却中", f"冷却期内应拒绝: {reason}"

    clock["t"] = 1700.0  # 700 秒后，冷却结束
    allowed, reason = gate.allow("第二段完全不一样的中文内容喵")
    assert allowed, f"冷却后应允许: {reason}"
    gate.record("第二段完全不一样的中文内容喵")

    clock["t"] = 2400.0
    allowed, reason = gate.allow("第三段完全不一样的中文内容喵")
    assert allowed, "配额内应允许"
    gate.record("第三段完全不一样的中文内容喵")

    clock["t"] = 3100.0
    allowed, reason = gate.allow("第四段完全不一样的中文内容喵")
    assert not allowed and reason == "小时配额已满", f"超出小时配额应拒绝: {reason}"

    # 4.1 开关关闭时全部拒绝
    gate.enabled = False
    allowed, reason = gate.allow("第五段完全不一样的中文内容喵")
    assert not allowed and reason == "开关关闭", "总开关应生效"
    gate.enabled = True

    # 4.2 敏感内容即使没评论过也拒绝
    gate2 = mod.CommentGate(cooldown_seconds=10, max_per_hour=6, now_fn=lambda: 1000.0)
    allowed, reason = gate2.allow("password=abc123")
    assert not allowed and reason == "内容sensitive", "敏感内容应被 gate 拒绝"

    # 5. 状态持久化契约：to_state → load_state 往返
    gate3 = mod.CommentGate(cooldown_seconds=600, max_per_hour=3, now_fn=lambda: 1000.0)
    gate3.record("一段值得记住的中文内容喵")
    state = gate3.to_state()
    gate4 = mod.CommentGate(cooldown_seconds=600, max_per_hour=3, now_fn=lambda: 1000.0)
    gate4.load_state(state)
    allowed, reason = gate4.allow("一段值得记住的中文内容喵")
    assert not allowed and reason == "近期已评论过", "恢复状态后仍应去重"
    gate5 = mod.CommentGate(cooldown_seconds=600, max_per_hour=3, now_fn=lambda: 1000.0)
    gate5.load_state({"enabled": False})
    assert_eq(gate5.enabled, False, "开关状态应可恢复")

    print("全部测试通过 ✅")


if __name__ == "__main__":
    main()
