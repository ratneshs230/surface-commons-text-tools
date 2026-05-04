"""Text Tools — Commons guild's stdlib-only text utilities bundle.

Replaces a stack of small SaaS / browser-tab tools (case-converter,
word-counter, find-replace, regex-tester, html-stripper, diff-checker).
All endpoints pure-Python, stdlib + ``regex`` only — no model calls, no
external services.

Endpoints (all under ``/api/v1/commons/text``):

- ``POST /case``         — convert case (upper/lower/title/sentence/camel/snake/kebab/pascal). FREE.
- ``POST /stats``        — chars/words/sentences/paragraphs/reading-time. FREE.
- ``POST /replace``      — find-replace (literal or regex), case toggle, count cap. 1 SC.
- ``POST /regex-test``   — regex matches with positions + groups. 1 SC.
- ``POST /strip``        — strip HTML / Markdown / both → plain text. 1 SC.
- ``POST /diff``         — unified diff between two strings. 1 SC.
- ``GET  /health``       — public probe.

Auth: free endpoints take no auth (small, abuse-bounded by input size).
Paid endpoints require ``X-Service-Key`` + ``X-Agent-Id`` and charge SC.
"""
from __future__ import annotations

import difflib
import html as _html
import json
import os
import re
import time
import uuid
from html.parser import HTMLParser
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.service_auth import require_service_key_and_agent
from app.models.agent import Agent
from app.services.ledger import (
    InsufficientFundsError,
    record_transaction,
)


router = APIRouter(prefix="/api/v1/commons/text", tags=["commons-text"])


# ── SC pricing ────────────────────────────────────────────────────────────
SC_COST = {
    "case": 0.0,
    "stats": 0.0,
    "replace": float(os.environ.get("TEXT_SC_REPLACE", "1")),
    "regex_test": float(os.environ.get("TEXT_SC_REGEX_TEST", "1")),
    "strip": float(os.environ.get("TEXT_SC_STRIP", "1")),
    "diff": float(os.environ.get("TEXT_SC_DIFF", "1")),
}

MAX_TEXT_BYTES = int(os.environ.get("TEXT_MAX_INPUT_BYTES", str(512 * 1024)))  # 512 KB
MAX_PATTERN_LEN = int(os.environ.get("TEXT_MAX_PATTERN_LEN", "2048"))
MAX_REGEX_MATCHES = int(os.environ.get("TEXT_MAX_REGEX_MATCHES", "1000"))
REGEX_TIMEOUT_S = float(os.environ.get("TEXT_REGEX_TIMEOUT_S", "1.0"))
WORDS_PER_MIN = int(os.environ.get("TEXT_READ_WPM", "230"))


# ── input guards ──────────────────────────────────────────────────────────

def _check_size(s: str, *, name: str = "text", limit: int = MAX_TEXT_BYTES) -> None:
    if len(s.encode("utf-8")) > limit:
        raise HTTPException(
            status_code=413,
            detail={
                "error": "input_too_large",
                "field": name,
                "limit_bytes": limit,
            },
        )


# ── ledger / logging ──────────────────────────────────────────────────────

async def _log_tool_call(
    db: AsyncSession,
    *,
    agent_id: str,
    tool_name: str,
    input_summary: dict,
    output_len: int,
    sc_cost: float,
    latency_ms: int,
    error: str | None = None,
) -> None:
    await db.execute(
        text(
            """
            INSERT INTO tool_calls
                (id, agent_id, tool_name, input_json, output_len,
                 sc_cost, latency_ms, error, created_at)
            VALUES
                (:id, :agent_id, :tool_name, CAST(:input_json AS JSONB),
                 :output_len, :sc_cost, :latency_ms, :error, NOW())
            """
        ),
        {
            "id": str(uuid.uuid4()),
            "agent_id": agent_id,
            "tool_name": tool_name,
            "input_json": json.dumps(input_summary, default=str),
            "output_len": output_len,
            "sc_cost": sc_cost,
            "latency_ms": latency_ms,
            "error": error,
        },
    )


async def _charge(
    db: AsyncSession,
    agent: Agent,
    *,
    amount: float,
    tool: str,
    ref: str,
) -> float:
    if amount <= 0:
        return 0.0
    try:
        await record_transaction(
            db,
            agent_id=agent.id,
            delta=-amount,
            reason=f"commons.text.{tool}",
            ref_id=ref,
            note=f"Text Tools: {tool}",
        )
    except InsufficientFundsError as e:
        raise HTTPException(
            status_code=402,
            detail={
                "error": "insufficient_sc",
                "balance": e.balance,
                "needed": amount,
                "tool": f"commons.text.{tool}",
            },
        )
    return amount


# ── case convert ──────────────────────────────────────────────────────────

CaseMode = Literal[
    "upper", "lower", "title", "sentence",
    "camel", "snake", "kebab", "pascal",
]


def _split_words(s: str) -> list[str]:
    """Split arbitrary text into word tokens, regardless of separator style.

    Handles spaces, dashes, underscores, and camelCase boundaries.
    """
    # Insert spaces at camelCase boundaries: aB → a B, ABc → A Bc
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", s)
    parts = re.split(r"[\s\-_]+", s)
    return [p for p in parts if p]


def convert_case(s: str, mode: str) -> str:
    if mode == "upper":
        return s.upper()
    if mode == "lower":
        return s.lower()
    if mode == "title":
        return s.title()
    if mode == "sentence":
        # Lowercase everything, then capitalise first letter after each
        # sentence terminator. Preserves whitespace.
        lowered = s.lower()
        out = []
        capitalize_next = True
        for ch in lowered:
            if capitalize_next and ch.isalpha():
                out.append(ch.upper())
                capitalize_next = False
            else:
                out.append(ch)
            if ch in ".!?":
                capitalize_next = True
        return "".join(out)
    words = _split_words(s)
    if not words:
        return ""
    if mode == "camel":
        return words[0].lower() + "".join(w.capitalize() for w in words[1:])
    if mode == "pascal":
        return "".join(w.capitalize() for w in words)
    if mode == "snake":
        return "_".join(w.lower() for w in words)
    if mode == "kebab":
        return "-".join(w.lower() for w in words)
    raise HTTPException(status_code=400, detail={"error": "unknown_mode", "mode": mode})


class _CaseBody(BaseModel):
    text: str = Field(..., max_length=MAX_TEXT_BYTES)
    mode: CaseMode


@router.post("/case")
async def case(body: Annotated[_CaseBody, ...]) -> dict:
    _check_size(body.text)
    return {
        "mode": body.mode,
        "text": convert_case(body.text, body.mode),
        "sc_charged": 0.0,
    }


# ── stats ────────────────────────────────────────────────────────────────

class _StatsBody(BaseModel):
    text: str = Field(..., max_length=MAX_TEXT_BYTES)


def compute_stats(s: str) -> dict:
    chars = len(s)
    chars_no_space = sum(1 for c in s if not c.isspace())
    words_list = re.findall(r"\b[\w']+\b", s, flags=re.UNICODE)
    words = len(words_list)
    # Sentences: split on . ! ? terminators, drop empties.
    sentences = len([p for p in re.split(r"[.!?]+", s) if p.strip()])
    # Paragraphs: separated by blank lines.
    paragraphs = len([p for p in re.split(r"\n\s*\n", s.strip()) if p.strip()]) if s.strip() else 0
    reading_time_min = round(words / WORDS_PER_MIN, 2) if words else 0.0
    return {
        "chars": chars,
        "chars_no_space": chars_no_space,
        "words": words,
        "sentences": sentences,
        "paragraphs": paragraphs,
        "reading_time_min": reading_time_min,
    }


@router.post("/stats")
async def stats(body: Annotated[_StatsBody, ...]) -> dict:
    _check_size(body.text)
    out = compute_stats(body.text)
    out["sc_charged"] = 0.0
    return out


# ── replace ──────────────────────────────────────────────────────────────

class _ReplaceBody(BaseModel):
    text: str = Field(..., max_length=MAX_TEXT_BYTES)
    pattern: str = Field(..., min_length=1, max_length=MAX_PATTERN_LEN)
    replacement: str = Field("", max_length=MAX_PATTERN_LEN)
    regex: bool = False
    case_sensitive: bool = True
    count: int = Field(0, ge=0, le=1_000_000)  # 0 = all


@router.post("/replace")
async def replace(
    body: Annotated[_ReplaceBody, ...],
    db: AsyncSession = Depends(get_db),
    agent: Agent = Depends(require_service_key_and_agent),
) -> dict:
    _check_size(body.text)
    t0 = time.monotonic()
    ref = str(uuid.uuid4())
    cost = SC_COST["replace"]
    await _charge(db, agent, amount=cost, tool="replace", ref=ref)

    try:
        if body.regex:
            flags = 0 if body.case_sensitive else re.IGNORECASE
            try:
                pat = re.compile(body.pattern, flags)
            except re.error as e:
                raise HTTPException(
                    status_code=400,
                    detail={"error": "bad_regex", "msg": str(e)[:200]},
                )
            count_cap = body.count if body.count > 0 else 0
            new_text, n = pat.subn(body.replacement, body.text, count=count_cap)
        else:
            if body.case_sensitive:
                if body.count > 0:
                    new_text = body.text.replace(
                        body.pattern, body.replacement, body.count
                    )
                else:
                    new_text = body.text.replace(body.pattern, body.replacement)
                # str.replace gives no count; compute via slicing logic.
                if body.count > 0:
                    # Best-effort: count occurrences replaced (capped).
                    raw = body.text.count(body.pattern)
                    n = min(raw, body.count)
                else:
                    n = body.text.count(body.pattern)
            else:
                # Case-insensitive literal replace via regex with escape.
                pat = re.compile(re.escape(body.pattern), re.IGNORECASE)
                count_cap = body.count if body.count > 0 else 0
                new_text, n = pat.subn(body.replacement, body.text, count=count_cap)

        out = {
            "text": new_text,
            "count": n,
            "regex": body.regex,
            "case_sensitive": body.case_sensitive,
            "sc_charged": cost,
        }
        await _log_tool_call(
            db,
            agent_id=agent.id,
            tool_name="commons.text.replace",
            input_summary={
                "in_bytes": len(body.text),
                "pattern_len": len(body.pattern),
                "regex": body.regex,
                "case_sensitive": body.case_sensitive,
                "count_cap": body.count,
                "replaced": n,
            },
            output_len=len(new_text),
            sc_cost=cost,
            latency_ms=int((time.monotonic() - t0) * 1000),
        )
        await db.commit()
        return out
    except HTTPException:
        await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=500,
            detail={"error": "replace_failed", "msg": str(e)[:200]},
        )


# ── regex test ────────────────────────────────────────────────────────────

class _RegexTestBody(BaseModel):
    pattern: str = Field(..., min_length=1, max_length=MAX_PATTERN_LEN)
    text: str = Field(..., max_length=MAX_TEXT_BYTES)
    flags: str | None = Field(None, max_length=16)  # subset of "imsx"


def _parse_flags(s: str | None) -> int:
    if not s:
        return 0
    out = 0
    table = {
        "i": re.IGNORECASE,
        "m": re.MULTILINE,
        "s": re.DOTALL,
        "x": re.VERBOSE,
        "u": re.UNICODE,
        "a": re.ASCII,
    }
    for ch in s.lower():
        if ch in table:
            out |= table[ch]
        else:
            raise HTTPException(
                status_code=400,
                detail={"error": "unknown_flag", "flag": ch},
            )
    return out


@router.post("/regex-test")
async def regex_test(
    body: Annotated[_RegexTestBody, ...],
    db: AsyncSession = Depends(get_db),
    agent: Agent = Depends(require_service_key_and_agent),
) -> dict:
    _check_size(body.text)
    t0 = time.monotonic()
    ref = str(uuid.uuid4())
    cost = SC_COST["regex_test"]
    await _charge(db, agent, amount=cost, tool="regex_test", ref=ref)

    try:
        flags = _parse_flags(body.flags)
        try:
            pat = re.compile(body.pattern, flags)
        except re.error as e:
            raise HTTPException(
                status_code=400,
                detail={"error": "bad_regex", "msg": str(e)[:200]},
            )

        matches: list[dict] = []
        truncated = False
        for i, m in enumerate(pat.finditer(body.text)):
            if i >= MAX_REGEX_MATCHES:
                truncated = True
                break
            matches.append({
                "match": m.group(0),
                "start": m.start(),
                "end": m.end(),
                "groups": list(m.groups()),
                "named": {k: v for k, v in m.groupdict().items()},
            })

        out = {
            "pattern": body.pattern,
            "flags": body.flags or "",
            "match_count": len(matches),
            "matches": matches,
            "truncated": truncated,
            "sc_charged": cost,
        }
        await _log_tool_call(
            db,
            agent_id=agent.id,
            tool_name="commons.text.regex_test",
            input_summary={
                "in_bytes": len(body.text),
                "pattern_len": len(body.pattern),
                "flags": body.flags or "",
                "match_count": len(matches),
                "truncated": truncated,
            },
            output_len=len(matches),
            sc_cost=cost,
            latency_ms=int((time.monotonic() - t0) * 1000),
        )
        await db.commit()
        return out
    except HTTPException:
        await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=500,
            detail={"error": "regex_test_failed", "msg": str(e)[:200]},
        )


# ── strip ────────────────────────────────────────────────────────────────

class _StripBody(BaseModel):
    text: str = Field(..., max_length=MAX_TEXT_BYTES)
    mode: Literal["html", "markdown", "both"] = "both"


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0
        self._skip_tags = {"script", "style"}

    def handle_starttag(self, tag: str, attrs):
        if tag.lower() in self._skip_tags:
            self._skip_depth += 1

    def handle_endtag(self, tag: str):
        if tag.lower() in self._skip_tags and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str):
        if self._skip_depth == 0:
            self._chunks.append(data)

    def text(self) -> str:
        return "".join(self._chunks)


def strip_html(s: str) -> str:
    p = _HTMLTextExtractor()
    p.feed(s)
    p.close()
    out = p.text()
    # Decode any leftover entities that the parser missed.
    out = _html.unescape(out)
    # Collapse runs of whitespace.
    out = re.sub(r"[ \t]+", " ", out)
    out = re.sub(r"\n[ \t]*", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def strip_markdown(s: str) -> str:
    # Code fences (multi-line) — keep contents.
    s = re.sub(r"```[a-zA-Z0-9_+-]*\n?", "", s)
    s = re.sub(r"```", "", s)
    # Inline code
    s = re.sub(r"`([^`]*)`", r"\1", s)
    # Images: ![alt](url) → alt
    s = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", s)
    # Links: [text](url) → text
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", s)
    # Headings: leading #s
    s = re.sub(r"^\s{0,3}#{1,6}\s+", "", s, flags=re.MULTILINE)
    # Blockquotes
    s = re.sub(r"^\s{0,3}>\s?", "", s, flags=re.MULTILINE)
    # List bullets / ordered prefixes
    s = re.sub(r"^\s*[-*+]\s+", "", s, flags=re.MULTILINE)
    s = re.sub(r"^\s*\d+\.\s+", "", s, flags=re.MULTILINE)
    # Horizontal rules
    s = re.sub(r"^\s*([-*_])\1{2,}\s*$", "", s, flags=re.MULTILINE)
    # Bold / italic / strikethrough markers — keep inner text.
    s = re.sub(r"\*\*\*([^*]+)\*\*\*", r"\1", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
    s = re.sub(r"\*([^*]+)\*", r"\1", s)
    s = re.sub(r"___([^_]+)___", r"\1", s)
    s = re.sub(r"__([^_]+)__", r"\1", s)
    s = re.sub(r"(?<!\w)_([^_]+)_(?!\w)", r"\1", s)
    s = re.sub(r"~~([^~]+)~~", r"\1", s)
    # Collapse blank lines
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


@router.post("/strip")
async def strip(
    body: Annotated[_StripBody, ...],
    db: AsyncSession = Depends(get_db),
    agent: Agent = Depends(require_service_key_and_agent),
) -> dict:
    _check_size(body.text)
    t0 = time.monotonic()
    ref = str(uuid.uuid4())
    cost = SC_COST["strip"]
    await _charge(db, agent, amount=cost, tool="strip", ref=ref)

    try:
        s = body.text
        if body.mode in ("html", "both"):
            s = strip_html(s)
        if body.mode in ("markdown", "both"):
            s = strip_markdown(s)

        out = {
            "mode": body.mode,
            "text": s,
            "in_bytes": len(body.text),
            "out_bytes": len(s),
            "sc_charged": cost,
        }
        await _log_tool_call(
            db,
            agent_id=agent.id,
            tool_name="commons.text.strip",
            input_summary={
                "mode": body.mode,
                "in_bytes": len(body.text),
                "out_bytes": len(s),
            },
            output_len=len(s),
            sc_cost=cost,
            latency_ms=int((time.monotonic() - t0) * 1000),
        )
        await db.commit()
        return out
    except HTTPException:
        await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=500,
            detail={"error": "strip_failed", "msg": str(e)[:200]},
        )


# ── diff ─────────────────────────────────────────────────────────────────

class _DiffBody(BaseModel):
    a: str = Field(..., max_length=MAX_TEXT_BYTES)
    b: str = Field(..., max_length=MAX_TEXT_BYTES)
    a_label: str = Field("a", max_length=128)
    b_label: str = Field("b", max_length=128)
    context: int = Field(3, ge=0, le=20)


@router.post("/diff")
async def diff(
    body: Annotated[_DiffBody, ...],
    db: AsyncSession = Depends(get_db),
    agent: Agent = Depends(require_service_key_and_agent),
) -> dict:
    _check_size(body.a, name="a")
    _check_size(body.b, name="b")
    t0 = time.monotonic()
    ref = str(uuid.uuid4())
    cost = SC_COST["diff"]
    await _charge(db, agent, amount=cost, tool="diff", ref=ref)

    try:
        a_lines = body.a.splitlines(keepends=True)
        b_lines = body.b.splitlines(keepends=True)
        diff_lines = list(
            difflib.unified_diff(
                a_lines,
                b_lines,
                fromfile=body.a_label,
                tofile=body.b_label,
                n=body.context,
            )
        )
        added = sum(1 for ln in diff_lines if ln.startswith("+") and not ln.startswith("+++"))
        removed = sum(1 for ln in diff_lines if ln.startswith("-") and not ln.startswith("---"))

        diff_text = "".join(diff_lines)
        out = {
            "diff": diff_text,
            "added": added,
            "removed": removed,
            "identical": added == 0 and removed == 0,
            "sc_charged": cost,
        }
        await _log_tool_call(
            db,
            agent_id=agent.id,
            tool_name="commons.text.diff",
            input_summary={
                "a_bytes": len(body.a),
                "b_bytes": len(body.b),
                "added": added,
                "removed": removed,
            },
            output_len=len(diff_text),
            sc_cost=cost,
            latency_ms=int((time.monotonic() - t0) * 1000),
        )
        await db.commit()
        return out
    except HTTPException:
        await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        raise HTTPException(
            status_code=500,
            detail={"error": "diff_failed", "msg": str(e)[:200]},
        )


# ── /health ──────────────────────────────────────────────────────────────

@router.get("/health")
async def health() -> dict:
    return {
        "service": "commons.text",
        "version": "1.0.0",
        "available": {"stdlib": True},
        "all_available": True,
        "sc_cost": SC_COST,
        "limits": {
            "max_input_bytes": MAX_TEXT_BYTES,
            "max_pattern_len": MAX_PATTERN_LEN,
            "max_regex_matches": MAX_REGEX_MATCHES,
            "words_per_min": WORDS_PER_MIN,
        },
        "case_modes": [
            "upper", "lower", "title", "sentence",
            "camel", "snake", "kebab", "pascal",
        ],
        "strip_modes": ["html", "markdown", "both"],
    }
