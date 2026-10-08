"""塔罗占卜核心逻辑（纯函数，独立于 SDK 与系统 API，便于测试）

职责：牌阵定义、洗牌抽牌（含正逆位）、无 LLM 时的诚实降级解读。
牌面数据在 `_tarot_data.py`（构建脚本生成）。
"""

from __future__ import annotations

import random
from typing import Any, Optional

try:
    from ._tarot_data import CARDS
except ImportError:  # 独立测试加载（tests/ 直接按文件加载本模块）
    from _tarot_data import CARDS

# 牌阵定义：位置含义来自传统塔罗通用解读框架（公共领域）
SPREADS: dict[str, dict[str, Any]] = {
    "one": {
        "name": "单张",
        "sub": "核心指引",
        "positions": ["核心之牌"],
    },
    "three": {
        "name": "三张",
        "sub": "过去 · 现在 · 未来",
        "positions": ["过去", "现在", "未来"],
    },
    "five": {
        "name": "五张",
        "sub": "问题剖析",
        "positions": ["现状", "面临的挑战", "根源 / 原因", "事态发展", "最终结果"],
    },
}

_MAX_HISTORY = 12


def spread_ids() -> list[str]:
    return list(SPREADS.keys())


def spread_meta() -> list[dict[str, Any]]:
    return [
        {"id": sid, "name": s["name"], "sub": s["sub"], "count": len(s["positions"])}
        for sid, s in SPREADS.items()
    ]


def draw(spread_id: str, question: str = "", rng: Optional[random.Random] = None) -> Optional[dict[str, Any]]:
    """洗牌抽牌：不放回抽样 + 每张 50% 逆位。返回 None 表示未知牌阵。"""
    spread = SPREADS.get(spread_id)
    if spread is None:
        return None
    rng = rng or random
    count = len(spread["positions"])
    picked = rng.sample(CARDS, count)
    cards = []
    for card, position in zip(picked, spread["positions"]):
        cards.append({
            "n": card["n"],
            "name": card["name"],
            "en": card["en"],
            "arcana": card["arcana"],
            "up": list(card["up"]),
            "rev": list(card["rev"]),
            "reversed": bool(rng.random() < 0.5),
            "position": position,
            "img": f"tarot/c{card['n']:02d}.jpg",
        })
    return {
        "spread": spread_id,
        "spread_name": spread["name"],
        "question": (question or "").strip()[:200],
        "cards": cards,
    }


def card_lines(drawn: dict[str, Any]) -> list[str]:
    """把一次抽牌压成给 LLM 的事实清单（只有真实牌义关键词，没有编造成分）。"""
    lines = []
    if drawn.get("question"):
        lines.append(f"问题：{drawn['question']}")
    lines.append(f"牌阵：{drawn.get('spread_name', '')}")
    for c in drawn.get("cards", []):
        orient = "逆位" if c["reversed"] else "正位"
        keys = "、".join(c["rev"] if c["reversed"] else c["up"])
        lines.append(f"【{c['position']}】{c['name']}（{orient}）——{keys}")
    return lines


def fallback_reading(drawn: dict[str, Any]) -> str:
    """LLM 不可用时的降级解读：只复述真实牌义关键词，不装作在解读。"""
    head = "本喵的解读灵感暂时连不上模型，先把每张牌的传统牌义摆给主人看喵：\n"
    body = "\n".join(f"· {line}" for line in card_lines(drawn))
    tail = "\n等模型回来说话了，主人再点一次「猫娘解读」，本喵给你连起来讲喵。"
    return head + body + tail
