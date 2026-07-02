"""Plain-text notification previews for Fetch push alerts.

The Fetch app can render rich ``card`` JSON in the thread, but APNs alert bodies
need a short human sentence. This module mirrors the iOS preview behavior enough
for plugin-created remote pushes so lock-screen banners do not expose raw JSON.
"""

from __future__ import annotations

import json
import re
from typing import Any

INPUT_CAP = 600
CARD_INPUT_CAP = 6_000
CARD_LANGUAGES = frozenset({"card", "cards", "hermes-card", "ui"})


def notification_body(raw: object, *, fallback: str, limit: int = 500) -> str:
    text = plain_text(raw)
    if not text:
        text = fallback
    return text[: max(0, limit)]


def plain_text(raw: object) -> str:
    card_probe = _normalize(str(raw or ""))[:CARD_INPUT_CAP]
    text = card_probe[:INPUT_CAP]

    card_summary = _generated_card_summary(text)
    if card_summary is None:
        card_summary = _generated_card_summary(card_probe)
    if card_summary is not None:
        text = card_summary
    elif _looks_like_card_fence(card_probe) or _looks_like_json_payload(card_probe):
        return ""

    text = _strip_cron_wrapper(text)
    lines = []
    for line in text.split("\n"):
        clean = _clean_line(line)
        if clean is not None:
            lines.append(clean)
    text = "\n".join(lines)
    text = _strip_inline(text)
    text = _collapse_whitespace(text)
    text = re.sub(r"[ \t]+[—–][ \t]+", ", ", text)
    text = re.sub(r"[—–]", "-", text)
    text = re.sub(r"\s+,", ",", text)
    text = re.sub(r",{2,}", ",", text)
    return _collapse_whitespace(text).strip(" ,")


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _strip_cron_wrapper(text: str) -> str:
    text = re.sub(
        r"\A\s*Cronjob Response:[^\n]*(?:\(job_id:[^)]*\))?[ \t]*(?:\n\s*\(job_id:[^)]*\))?[ \t]*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\A\s*(?:[-*_]=?\s*){3,}\n?", "", text)


def _generated_card_summary(input_text: str) -> str | None:
    trimmed = input_text.strip()
    candidate = _fenced_body(trimmed) if trimmed.startswith("```") else trimmed
    raw_json = _first_json_object(candidate)
    if raw_json is None:
        return None
    try:
        spec = json.loads(raw_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(spec, dict) or not _card_has_substance(spec):
        return None

    parts: list[str] = []
    _append(spec.get("title"), parts)
    _append(spec.get("subtitle"), parts)
    _append(spec.get("footer"), parts)

    for stat in _as_list(spec.get("stats"))[:3]:
        if isinstance(stat, dict):
            _append_joined([stat.get("label"), _flex(stat.get("value"))], parts)

    for item in _as_list(spec.get("items"))[:2]:
        if isinstance(item, dict):
            _append(item.get("title"), parts)
            _append(item.get("subtitle"), parts)
            _append(_flex(item.get("value")), parts)

    for card in _as_list(spec.get("cards"))[:2]:
        if isinstance(card, dict):
            _append(card.get("title"), parts)
            _append(card.get("subtitle"), parts)
            _append(card.get("badge"), parts)

    for block in _as_list(spec.get("blocks"))[:4]:
        if isinstance(block, dict) and _block_renders_content(block):
            _summarize_block(block, parts)

    summary = ", ".join(part for part in (_collapse_whitespace(p) for p in parts) if part)
    return summary


def _card_has_substance(spec: dict[str, Any]) -> bool:
    if spec.get("title") is not None:
        return True
    if _as_list(spec.get("items")) or _as_list(spec.get("stats")) or _as_list(spec.get("cards")):
        return True
    chart = spec.get("chart")
    if isinstance(chart, dict) and _chart_kind(chart):
        return True
    return any(isinstance(block, dict) and _block_renders_content(block) for block in _as_list(spec.get("blocks")))


def _summarize_block(block: dict[str, Any], parts: list[str]) -> None:
    kind = _block_kind(block)
    if kind == "text":
        _append(block.get("title"), parts)
        _append(block.get("text") or block.get("body"), parts)
    elif kind == "metric":
        _append_joined([block.get("label"), _flex(block.get("value"))], parts)
    elif kind == "stats":
        for stat in _as_list(block.get("stats"))[:3]:
            if isinstance(stat, dict):
                _append_joined([stat.get("label"), _flex(stat.get("value"))], parts)
    elif kind == "rows":
        for item in _as_list(block.get("items"))[:2]:
            if isinstance(item, dict):
                _append(item.get("title"), parts)
                _append(item.get("subtitle"), parts)
    elif kind == "progress":
        _append(block.get("caption"), parts)
    elif kind == "checklist":
        for step in _as_list(block.get("steps"))[:3]:
            if isinstance(step, dict):
                _append(step.get("title"), parts)
    elif kind == "bars":
        for bar in _as_list(block.get("bars"))[:3]:
            if isinstance(bar, dict):
                _append(bar.get("label"), parts)
    elif kind == "compare":
        for option in _as_list(block.get("options"))[:3]:
            if isinstance(option, dict):
                _append(option.get("name"), parts)


def _block_kind(block: dict[str, Any]) -> str | None:
    raw_type = block.get("type")
    if raw_type is not None:
        normalized = "".join(ch for ch in str(raw_type).lower() if ch.isalpha())
        explicit = {
            "text": "text",
            "note": "text",
            "paragraph": "text",
            "body": "text",
            "prose": "text",
            "metric": "metric",
            "bignumber": "metric",
            "kpi": "metric",
            "number": "metric",
            "hero": "metric",
            "stat": "metric",
            "stats": "stats",
            "statgrid": "stats",
            "statcolumns": "stats",
            "statistics": "stats",
            "metrics": "stats",
            "rows": "rows",
            "list": "rows",
            "rowlist": "rows",
            "links": "rows",
            "progress": "progress",
            "progressbar": "progress",
            "bar": "progress",
            "checklist": "checklist",
            "checks": "checklist",
            "steps": "checklist",
            "todo": "checklist",
            "tasks": "checklist",
            "checkbox": "checklist",
            "bars": "bars",
            "breakdown": "bars",
            "distribution": "bars",
            "barlist": "bars",
            "split": "bars",
            "compare": "compare",
            "comparison": "compare",
            "options": "compare",
            "versus": "compare",
            "vs": "compare",
            "choices": "compare",
            "divider": "divider",
            "rule": "divider",
            "separator": "divider",
            "hr": "divider",
            "line": "divider",
        }.get(normalized)
        if explicit:
            return explicit
        if normalized == "chart" and _chart_kind(block.get("chart")):
            return "chart"

    if _chart_kind(block.get("chart")):
        return "chart"
    if _as_list(block.get("steps")):
        return "checklist"
    if _as_list(block.get("options")):
        return "compare"
    if _as_list(block.get("bars")):
        return "bars"
    if _as_list(block.get("stats")):
        return "stats"
    if _as_list(block.get("items")):
        return "rows"
    if block.get("percent") is not None:
        return "progress"
    if block.get("value") is not None:
        return "metric"
    if block.get("text") is not None or block.get("body") is not None:
        return "text"
    return None


def _block_renders_content(block: dict[str, Any]) -> bool:
    kind = _block_kind(block)
    if kind == "text":
        return block.get("title") is not None or block.get("text") is not None or block.get("body") is not None
    if kind == "metric":
        return block.get("value") is not None or block.get("label") is not None or block.get("delta") is not None
    if kind == "stats":
        return bool(_as_list(block.get("stats")))
    if kind == "rows":
        return bool(_as_list(block.get("items")))
    if kind == "progress":
        return block.get("percent") is not None
    if kind == "checklist":
        return bool(_as_list(block.get("steps")))
    if kind == "bars":
        return bool(_as_list(block.get("bars")))
    if kind == "compare":
        return bool(_as_list(block.get("options")))
    if kind == "chart":
        return _chart_kind(block.get("chart"))
    if kind == "divider":
        return True
    return False


def _chart_kind(value: object) -> bool:
    return isinstance(value, dict) and bool(value.get("type") or value.get("kind"))


def _fenced_body(text: str) -> str:
    lines = text.split("\n")
    return "\n".join(line for line in lines[1:] if not line.strip().startswith("```"))


def _looks_like_card_fence(text: str) -> bool:
    first = text.lstrip().split("\n", 1)[0].strip()
    if not first.startswith("```"):
        return False
    language = first[3:].strip().lower()
    return language in CARD_LANGUAGES


def _first_json_object(text: str) -> str | None:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaping = False
    for index, ch in enumerate(text[start:], start=start):
        if in_string:
            if escaping:
                escaping = False
            elif ch == "\\":
                escaping = True
            elif ch == '"':
                in_string = False
        elif ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _is_json_payload(text: str) -> bool:
    trimmed = text.strip()
    if not trimmed.startswith(("{", "[")):
        return False
    try:
        json.loads(trimmed)
    except json.JSONDecodeError:
        return False
    return True


def _looks_like_json_payload(text: str) -> bool:
    trimmed = text.strip()
    if _is_json_payload(trimmed):
        return True
    if trimmed.startswith("{"):
        return ":" in trimmed
    if trimmed.startswith("["):
        return "{" in trimmed and ":" in trimmed
    return False


def _clean_line(line: str) -> str | None:
    text = line.strip()
    if not text:
        return ""
    if text.startswith("```"):
        return ""
    compact = text.replace(" ", "")
    if len(compact) >= 3 and all(ch == compact[0] for ch in compact) and compact[0] in "-*_=":
        return None
    if "-" in compact and all(ch in "|-:" for ch in compact):
        return None
    if _is_machine_preamble(text):
        return None
    text = re.sub(r"^#{1,6}\s+", "", text)
    text = re.sub(r"^(>\s?)+", "", text)
    text = re.sub(r"^([-*+]|\d+[.)])\s+", "", text)
    return text.replace("|", " ")


def _is_machine_preamble(text: str) -> bool:
    lower = text.lower()
    return lower.startswith(("[important", "[system", "[internal", "[developer", "[tool", "[context"))


def _strip_inline(text: str) -> str:
    text = re.sub(r"!\[[^\]]*]\((?:[^()]|\([^)]*\))*\)", "", text)
    text = re.sub(r"\[([^\]]+)]\((?:[^()]|\([^)]*\))*\)", r"\1", text)
    text = re.sub(r"(\*\*|__)(.+?)\1", r"\2", text)
    text = re.sub(r"(?<![A-Za-z0-9])_(\S(?:.*?\S)?)_(?![A-Za-z0-9])", r"\1", text)
    text = re.sub(r"(?<!\*)\*(\S(?:.*?\S)?)\*(?!\*)", r"\1", text)
    text = re.sub(r"~~(.+?)~~", r"\1", text)
    text = text.replace("```", "")
    return re.sub(r"`([^`]+)`", r"\1", text)


def _append(value: object, parts: list[str]) -> None:
    text = _collapse_whitespace(str(value)) if value is not None else ""
    if text:
        parts.append(text)


def _append_joined(values: list[object], parts: list[str]) -> None:
    text = " ".join(_collapse_whitespace(str(value)) for value in values if value not in (None, ""))
    if text:
        parts.append(text)


def _flex(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else f"{value:.2f}"
    return str(value)


def _as_list(value: object) -> list:
    return value if isinstance(value, list) else []


def _collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()
