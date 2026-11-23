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

# ── v0.5.0 新增：更细的分流 ────────────────────────────────
# 报错栈：比一般代码更值得「要不要我看看」
_STACK_RE = re.compile(
    r"(Traceback \(most recent call last\)|\s+File \"[^\"]+\", line \d+|"
    r"\bat [\w.$]+\([\w./$]+:\d+:\d+\)|Error:.+\n\s+at )",
    re.MULTILINE,
)
# 表格：制表符分隔或多列 Markdown 表
_TABLE_RE = re.compile(r"(\t[^\t\n]*\t)|(^\s*\|.+\|\s*$)", re.MULTILINE)
# 命令行：整段像在终端里复制出来的
_COMMAND_RE = re.compile(
    r"^\s*(\$ |PS [A-Z]:\\|sudo |npm |npx |pnpm |pip |git |docker |kubectl |"
    r"curl |wget |cd |ls |dir |python |pytest |cargo |go build)",
    re.MULTILINE,
)
# 磁力/网盘直链
_MAGNET_RE = re.compile(r"^(magnet:\?|ed2k://|thunder://|ftp://)", re.IGNORECASE)
# 联系方式：能识别但**只给掩码**，不把完整号码推给模型
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]{2,}")
_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")

# 链接细分：仍是 kind=url，只是话术不一样（测试与旧行为都保持兼容）
_URL_SUB = [
    ("video", ("bilibili.com", "youtube.com", "youtu.be", "douyin", "ixigua", "vimeo", "netflix")),
    ("repo", ("github.com", "gitlab.com", "gitee.com", "bitbucket", "huggingface.co")),
    ("doc", ("docs.", "notion.so", "yuque.com", "feishu.cn", "confluence", "wikipedia",
             "arxiv.org", "doi.org", "zhihu.com")),
    ("shop", ("taobao.com", "tmall", "jd.com", "pinduoduo", "amazon.", "shopee")),
    ("social", ("weibo", "x.com", "twitter.com", "xiaohongshu", "reddit.com", "tieba")),
    ("image", (".png", ".jpg", ".jpeg", ".webp", ".gif")),
    ("file", (".pdf", ".zip", ".rar", ".7z", ".exe", ".msi", ".dmg", ".apk", ".epub")),
]

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
    "stack": "主人复制了一段报错喵，要不要本喵一起看看是哪一行出问题？{snippet}",
    "table": "主人复制了一张表格喵，要本喵帮忙整理成结论吗？{snippet}",
    "command": "主人复制了命令喵，要本喵解释一下它在干什么吗？{snippet}",
    "magnet": "主人复制了下载直链喵，本喵只帮你记着，不会自己去下哦：{snippet}",
    "contact": "主人复制了联系方式喵（本喵只看到掩码）：{snippet}，要记进备忘吗？",
}

# 链接细分后的说法（kind=url + sub）
_URL_SUB_HINTS: dict[str, str] = {
    "video": "主人复制了一个视频链接喵，要本喵先帮你看看讲什么吗？{snippet}",
    "repo": "主人复制了一个代码仓库喵，要本喵帮你看看它是做什么的吗？{snippet}",
    "doc": "主人复制了一篇文档/资料喵，要本喵提炼要点吗？{snippet}",
    "shop": "主人复制了商品链接喵，要本喵帮你比一下值不值得买吗？{snippet}",
    "social": "主人复制了一条社交平台的链接喵，要本喵看看大家都在说什么吗？{snippet}",
    "image": "主人复制了一张图的地址喵，要本喵看看是什么吗？{snippet}",
    "file": "主人复制了一个文件链接喵，要本喵看看是什么文件吗？{snippet}",
}

# 可被面板单独关掉的分类（敏感内容永远不可开启）
TOGGLEABLE_KINDS = ("url", "code", "json", "stack", "table", "command", "magnet",
                    "contact", "english", "number", "text", "long")

# 长度边界：太短多是误触。
# 读取上限默认 0 = 不截断：主人复制了多少，猫娘就读多少（面板「限速」页可改）。
_MIN_LENGTH = 4
_MAX_TRACK_LENGTH = 8000
_DEFAULT_MAX_SNIPPET = 0


def _clip(text: str, max_chars: int = _DEFAULT_MAX_SNIPPET) -> str:
    """按上限裁剪文本；``max_chars <= 0`` 表示不限制。

    真被裁了会补一个省略号，免得看上去像"内容本来就这么短"。
    """
    if max_chars and max_chars > 0 and len(text) > max_chars:
        return text[:max_chars] + "…"
    return text


def _url_sub(text: str) -> str:
    lowered = text.lower()
    for name, needles in _URL_SUB:
        if any(needle in lowered for needle in needles):
            return name
    return ""


def mask_contact(text: str) -> str:
    """联系方式只给掩码：邮箱留首字与域名，手机号留前 3 后 2。"""
    def _phone(match):
        number = match.group(1)
        return f"{number[:3]}****{number[-2:]}"

    def _mail(match):
        addr = match.group(0)
        head, _, domain = addr.partition("@")
        return f"{head[:1]}***@{domain}"

    masked = _PHONE_RE.sub(_phone, text or "")
    return _EMAIL_RE.sub(_mail, masked)


def classify_content(text: str, max_snippet: int = _DEFAULT_MAX_SNIPPET) -> dict[str, Any]:
    """给剪贴板内容分类，返回 ``{"kind", "snippet", "label"[, "sub"]}``。

    ``max_snippet`` 控制返回片段的长度上限，0（默认）表示原样返回不截断。

    敏感检查必须放在最前：超长文本（>8000 字）里也可能藏着密钥，
    一旦先按长度归为 long，片段（可能正好含密码）就会被推送出去。
    """
    cleaned = (text or "").strip()
    sub = ""
    if _SENSITIVE_RE.search(cleaned):
        return {"kind": "sensitive", "snippet": "", "label": "敏感内容"}
    if len(cleaned) > _MAX_TRACK_LENGTH:
        kind, snippet = "long", _clip(cleaned, max_snippet)
    elif _SENSITIVE_RE.search(cleaned):
        return {"kind": "sensitive", "snippet": "", "label": "敏感内容"}
    elif _MAGNET_RE.match(cleaned):
        kind, snippet = "magnet", _clip(cleaned, max_snippet)
    elif _URL_RE.search(cleaned) and len(cleaned) < 500 and cleaned.count(" ") <= 3:
        kind, snippet = "url", _clip(cleaned, max_snippet)
        sub = _url_sub(cleaned)
    elif _JSON_RE.match(cleaned):
        kind, snippet = "json", _clip(cleaned, max_snippet)
    elif _COMMAND_RE.search(cleaned) and len(cleaned) < 400 and "\n" not in cleaned.strip():
        kind, snippet = "command", _clip(cleaned, max_snippet)
    elif _STACK_RE.search(cleaned):
        # 报错栈仍算 code（旧行为），只多带一个 sub 让话术更准
        kind, snippet, sub = "code", _clip(cleaned, max_snippet), "stack"
    elif _CODE_HINT_RE.search(cleaned) or (_CODE_SHAPE_RE.search(cleaned) and len(cleaned) > 30):
        kind, snippet = "code", _clip(cleaned, max_snippet)
    elif _TABLE_RE.search(cleaned) and len(cleaned) < _MAX_TRACK_LENGTH:
        kind, snippet = "table", _clip(cleaned, max_snippet)
    elif _EMAIL_RE.search(cleaned) or _PHONE_RE.search(cleaned):
        kind, snippet = "contact", _clip(mask_contact(cleaned), max_snippet)
    elif len(cleaned) > 300:
        kind, snippet = "long", _clip(cleaned, max_snippet)
    elif len(cleaned) >= _MIN_LENGTH:
        ascii_letters = len("".join(_ASCII_LETTER_RE.findall(cleaned)))
        if ascii_letters >= max(6, int(len(cleaned) * 0.6)):
            kind = "english"
        elif _NUMBER_RE.match(cleaned):
            kind = "number"
        else:
            kind = "text"
        snippet = _clip(cleaned, max_snippet)
    else:
        return {"kind": "ignore", "snippet": "", "label": "太短，忽略"}
    out = {"kind": kind, "snippet": snippet, "label": _KIND_HINTS[kind].split("喵")[0]}
    if sub:
        out["sub"] = sub
        if kind == "code" and sub == "stack":
            out["label"] = "报错栈"
    return out


def is_sensitive(text: str) -> bool:
    return bool(_SENSITIVE_RE.search(text or ""))


def build_comment(kind: str, snippet: str, max_snippet: int = _DEFAULT_MAX_SNIPPET,
                  sub: str = "") -> Optional[str]:
    """生成给猫娘的推送文本；``sensitive`` / ``ignore`` 返回 ``None``。

    ``max_snippet`` 为 0（默认）时不截断，剪贴板里多长就推多长。
    """
    hint = _URL_SUB_HINTS.get(sub) if kind == "url" and sub else None
    if not hint and kind == "code" and sub == "stack":
        hint = _KIND_HINTS["stack"]
    hint = hint or _KIND_HINTS.get(kind)
    if not hint:
        return None
    short = (snippet or "").strip().replace("\r", " ").replace("\n", " ")
    if max_snippet and max_snippet > 0 and len(short) > max_snippet:
        short = short[:max_snippet] + "…"
    if not short:
        return None
    return hint.format(snippet=short)


def quiet_now(now: float, start: str, end: str) -> bool:
    """是否处在静音时段。``start``/``end`` 形如 "23:30"；支持跨零点。"""
    def _minutes(value: str) -> Optional[int]:
        try:
            hour, _, minute = str(value).partition(":")
            return int(hour) * 60 + int(minute)
        except Exception:
            return None

    begin, finish = _minutes(start), _minutes(end)
    if begin is None or finish is None or begin == finish:
        return False
    current = time.localtime(now)
    now_min = current.tm_hour * 60 + current.tm_min
    if begin < finish:
        return begin <= now_min < finish
    return now_min >= begin or now_min < finish


class CommentGate:
    """开口节奏控制：去重 + 冷却 + 每小时上限 + 开关。"""

    def __init__(
        self,
        cooldown_seconds: float = 5.0,
        max_per_hour: int = 30,
        max_tracked: int = 200,
        now_fn=time.time,
        disabled_kinds=None,
        quiet_start: str = "",
        quiet_end: str = "",
    ):
        self.cooldown_seconds = max(2.0, float(cooldown_seconds))
        self.max_per_hour = max(1, int(max_per_hour))
        self.max_tracked = max(10, int(max_tracked))
        self._now = now_fn
        self.recent_hashes: list[str] = []
        self.push_times: list[float] = []
        self.last_push_at: float = 0.0
        self.enabled: bool = True
        # 分类开关与静音时段：猫娘不该在他睡着的半夜为一段代码开口
        self.disabled_kinds: set[str] = {
            str(k) for k in (disabled_kinds or []) if str(k) in TOGGLEABLE_KINDS
        }
        self.quiet_start: str = str(quiet_start or "")
        self.quiet_end: str = str(quiet_end or "")

    def _hash(self, text: str) -> str:
        # 只哈希前 _MAX_TRACK_LENGTH 个字符：allow() 截断后算哈希、record() 用全文算哈希，
        # 同一段超长内容会因哈希不一致而重复搭话——统一在哈希内部截断。
        data = (text or "").strip()[:_MAX_TRACK_LENGTH]
        return hashlib.sha256(data.encode("utf-8", "ignore")).hexdigest()

    def allow(self, text: str) -> tuple[bool, str]:
        """判断是否应该开口；返回 ``(是否允许, 原因)``。"""
        if not self.enabled:
            return False, "开关关闭"
        cleaned = (text or "").strip()
        if len(cleaned) < _MIN_LENGTH:
            return False, "内容太短"
        info = classify_content(cleaned)
        if info["kind"] in ("sensitive", "ignore"):
            return False, f"内容{info['kind']}"
        if info["kind"] in self.disabled_kinds:
            return False, f"分类已关闭（{info['kind']}）"
        now = self._now()
        if quiet_now(now, self.quiet_start, self.quiet_end):
            return False, "静音时段"
        digest = self._hash(cleaned)
        if digest in self.recent_hashes:
            return False, "近期已评论过"
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
            "disabled_kinds": sorted(self.disabled_kinds),
            "quiet_start": self.quiet_start,
            "quiet_end": self.quiet_end,
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
        kinds = state.get("disabled_kinds")
        if isinstance(kinds, list):
            self.disabled_kinds = {str(k) for k in kinds if str(k) in TOGGLEABLE_KINDS}
        self.quiet_start = str(state.get("quiet_start") or "")[:5]
        self.quiet_end = str(state.get("quiet_end") or "")[:5]
