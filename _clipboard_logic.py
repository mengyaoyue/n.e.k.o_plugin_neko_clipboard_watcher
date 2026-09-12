"""剪贴板猫娘核心逻辑（纯函数，独立于 N.E.K.O SDK 与系统 API，便于测试）

职责：判断"猫娘要不要对刚复制的内容开口、用什么话题开口"。
不读剪贴板（那是平台层的事），只做内容分类、敏感过滤与节奏控制。
"""

from __future__ import annotations

import hashlib
import re
import time
from typing import Any, Optional

# 内容分类规则（按顺序命中）
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_CODE_HINT_RE = re.compile(
    r"(Traceback|Error|Exception|def |class |function |import |const |var |let |"
    r"SELECT .* FROM|#!|\{\"\w+\":|</\w+>|npm |pip install|git (clone|push|pull))",
    re.IGNORECASE,
)
_CODE_SHAPE_RE = re.compile(r"[{};()=]\s*$|\n\s{4,}", re.MULTILINE)
_JSON_RE = re.compile(r"^\s*[\[{].*[\]}]\s*$", re.DOTALL)
_NUMBER_RE = re.compile(r"^\s*[\d,.\s]+%?\s*$")
# 英文占比足够高才算英文句子（中文混排时优先按中文处理）
_ASCII_LETTER_RE = re.compile(r"[A-Za-z]")

# 敏感内容：疑似密码/密钥/令牌，猫娘看都不看
_SENSITIVE_RE = re.compile(
    r"(password|passwd|pwd|token|secret|api[_-]?key|access[_-]?key|private[_-]?key|"
    r"BEGIN (RSA|OPENSSH|EC) PRIVATE KEY|Authorization:|Bearer\s+\S+|"
    r"(?<![A-Za-z0-9])sk-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{20,})",
    re.IGNORECASE,
)

# 每个分类的猫娘搭话模板（{snippet} 会替换成内容片段）
_KIND_HINTS: dict[str, str] = {
    "url": "主人复制了一个链接喵，本喵可以帮你看里面是什么：{snippet}",
    "code": "喵？这段代码看起来有故事：{snippet}，需要本喵帮忙看看吗？",
    "json": "主人复制了一段结构化数据喵，要本喵帮你读一下吗？{snippet}",
    "english": "这段英文要本喵翻译一下吗喵？{snippet}",
    "number": "主人复制了数字：{snippet}，要算点什么吗喵？",
    "text": "主人刚刚复制了这段话喵：{snippet}",
    "long": "主人复制了好长一段内容喵，需要本喵总结一下吗？{snippet}",
}

# 长度边界：太短多是误触，太长塞给猫娘会吵
_MIN_LENGTH = 4
_MAX_PUSH_SNIPPET = 200
_MAX_TRACK_LENGTH = 8000


def classify_content(text: str) -> dict[str, Any]:
    """给剪贴板内容分类，返回 ``{"kind", "snippet", "label"}``。"""
    cleaned = (text or "").strip()
    if len(cleaned) > _MAX_TRACK_LENGTH:
        kind, snippet = "long", cleaned[:_MAX_PUSH_SNIPPET]
    elif _SENSITIVE_RE.search(cleaned):
        return {"kind": "sensitive", "snippet": "", "label": "敏感内容"}
    elif _URL_RE.search(cleaned) and len(cleaned) < 500 and cleaned.count(" ") <= 3:
        kind, snippet = "url", cleaned[:_MAX_PUSH_SNIPPET]
    elif _JSON_RE.match(cleaned):
        kind, snippet = "json", cleaned[:_MAX_PUSH_SNIPPET]
    elif _CODE_HINT_RE.search(cleaned) or (_CODE_SHAPE_RE.search(cleaned) and len(cleaned) > 30):
        kind, snippet = "code", cleaned[:_MAX_PUSH_SNIPPET]
    elif len(cleaned) > 300:
        kind, snippet = "long", cleaned[:_MAX_PUSH_SNIPPET]
    elif len(cleaned) >= _MIN_LENGTH:
        ascii_letters = len("".join(_ASCII_LETTER_RE.findall(cleaned)))
        if ascii_letters >= max(6, int(len(cleaned) * 0.6)):
            kind = "english"
        elif _NUMBER_RE.match(cleaned):
            kind = "number"
        else:
            kind = "text"
        snippet = cleaned[:_MAX_PUSH_SNIPPET]
    else:
        return {"kind": "ignore", "snippet": "", "label": "太短，忽略"}
    return {"kind": kind, "snippet": snippet, "label": _KIND_HINTS[kind].split("喵")[0]}


def is_sensitive(text: str) -> bool:
    return bool(_SENSITIVE_RE.search(text or ""))


def build_comment(kind: str, snippet: str) -> Optional[str]:
    """生成给猫娘的推送文本；``sensitive`` / ``ignore`` 返回 ``None``。"""
    hint = _KIND_HINTS.get(kind)
    if not hint:
        return None
    short = (snippet or "").strip().replace("\r", " ").replace("\n", " ")
    if len(short) > _MAX_PUSH_SNIPPET:
        short = short[:_MAX_PUSH_SNIPPET] + "…"
    if not short:
        return None
    return hint.format(snippet=short)


class CommentGate:
    """开口节奏控制：去重 + 冷却 + 每小时上限 + 开关。"""

    def __init__(
        self,
        cooldown_seconds: float = 5.0,
        max_per_hour: int = 30,
        max_tracked: int = 200,
        now_fn=time.time,
    ):
        self.cooldown_seconds = max(2.0, float(cooldown_seconds))
        self.max_per_hour = max(1, int(max_per_hour))
        self.max_tracked = max(10, int(max_tracked))
        self._now = now_fn
        self.recent_hashes: list[str] = []
        self.push_times: list[float] = []
        self.last_push_at: float = 0.0
        self.enabled: bool = True

    def _hash(self, text: str) -> str:
        return hashlib.sha256((text or "").strip().encode("utf-8", "ignore")).hexdigest()

    def allow(self, text: str) -> tuple[bool, str]:
        """判断是否应该开口；返回 ``(是否允许, 原因)``。"""
        if not self.enabled:
            return False, "开关关闭"
        cleaned = (text or "").strip()
        if len(cleaned) < _MIN_LENGTH:
            return False, "内容太短"
        if len(cleaned) > _MAX_TRACK_LENGTH:
            cleaned = cleaned[:_MAX_TRACK_LENGTH]
        info = classify_content(cleaned)
        if info["kind"] in ("sensitive", "ignore"):
            return False, f"内容{info['kind']}"
        digest = self._hash(cleaned)
        if digest in self.recent_hashes:
            return False, "近期已评论过"
        now = self._now()
        if now - self.last_push_at < self.cooldown_seconds:
            return False, "冷却中"
        self.push_times = [t for t in self.push_times if now - t < 3600.0]
        if len(self.push_times) >= self.max_per_hour:
            return False, "小时配额已满"
        return True, info["kind"]

    def record(self, text: str) -> None:
        """实际推送后登记：去重环 + 时间窗。"""
        digest = self._hash(text)
        if digest not in self.recent_hashes:
            self.recent_hashes.append(digest)
            if len(self.recent_hashes) > self.max_tracked:
                self.recent_hashes = self.recent_hashes[-self.max_tracked:]
        now = self._now()
        self.push_times = [t for t in self.push_times if now - t < 3600.0]
        self.push_times.append(now)
        self.last_push_at = now

    def to_state(self) -> dict[str, Any]:
        return {
            "recent_hashes": list(self.recent_hashes),
            "push_times": list(self.push_times),
            "last_push_at": self.last_push_at,
            "enabled": self.enabled,
        }

    def load_state(self, state: Any) -> None:
        if not isinstance(state, dict):
            return
        hashes = state.get("recent_hashes")
        if isinstance(hashes, list):
            self.recent_hashes = [str(h) for h in hashes][-self.max_tracked:]
        times = state.get("push_times")
        if isinstance(times, list):
            now = self._now()
            self.push_times = [float(t) for t in times if now - float(t) < 3600.0]
        self.last_push_at = float(state.get("last_push_at") or 0.0)
        self.enabled = bool(state.get("enabled", True))
