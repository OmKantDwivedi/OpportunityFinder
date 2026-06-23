"""Product-to-thread relevance matching and opportunity scoring.

Relevance: token overlap between the product's type/category/brand vocabulary
and the thread's keyword + title + subreddit (Jaccard-style, weighted toward
the product type).

Opportunity score (0-100):
    score = relevance * traffic_factor * position_factor * commission_factor
  - traffic_factor:   log-scaled monthly organic traffic (cap at 50k)
  - position_factor:  1.0 at #1 decaying to 0.55 at #10
  - commission_factor: mild boost for higher commission (cap at $50)
"""
from __future__ import annotations

import math
import re

_STOP = {"the", "a", "an", "and", "or", "for", "of", "best", "reddit", "review",
         "recommendation", "vs", "to", "in", "r"}


def _tokens(text: str) -> set[str]:
    return {t for t in re.sub(r"[^a-z0-9 ]", " ", text.lower()).split() if t and t not in _STOP}


def relevance(product: dict, keyword: str, thread_title: str, subreddit: str) -> float:
    """0..1 - how relevant a thread is to a product."""
    p_type = _tokens(product.get("type_kw", "") or product.get("name", ""))
    p_extra = _tokens(f"{product.get('category','')} {product.get('brand','')}")
    t_tokens = _tokens(f"{keyword} {thread_title} {subreddit}")
    if not p_type or not t_tokens:
        return 0.0
    type_overlap = len(p_type & t_tokens) / len(p_type)          # weight 0.8
    extra_overlap = len(p_extra & t_tokens) / max(len(p_extra), 1)  # weight 0.2
    return round(min(1.0, 0.8 * type_overlap + 0.2 * extra_overlap), 3)


def opportunity_score(rel: float, traffic: int, position: int, commission: float) -> float:
    traffic_factor = math.log10(max(traffic, 1) + 1) / math.log10(50_001)  # 0..1 at 50k visits
    position_factor = 1.0 - 0.05 * (max(1, min(position, 10)) - 1)          # 1.0 .. 0.55
    commission_factor = 0.8 + 0.2 * min(commission, 50) / 50                # 0.8 .. 1.0
    return round(100 * rel * traffic_factor * position_factor * commission_factor, 1)
