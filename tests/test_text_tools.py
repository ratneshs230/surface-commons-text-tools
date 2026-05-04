"""Tests for /commons/text — Text Tools bundle.

Two layers:

1. Pure-Python tests for the conversion / stats / strip / diff helpers —
   no auth, no DB, no network. These are the bulk of the suite since the
   tool is stdlib-only.

2. HTTP integration tests against a running surface-api at
   127.0.0.1:3000, gated on SURFACE_SERVICE_KEY + SURFACE_TEST_AGENT_ID
   for the paid endpoints. Free endpoints (/case, /stats, /health) are
   tested unauthenticated.
"""
from __future__ import annotations

import os

import httpx
import pytest

from app.api.commons.text_tools import (
    SC_COST,
    _parse_flags,
    compute_stats,
    convert_case,
    strip_html,
    strip_markdown,
)


BASE = os.environ.get("SURFACE_BASE_URL", "http://127.0.0.1:3000")
SVC_KEY = os.environ.get("SURFACE_SERVICE_KEY") or os.environ.get("SERVICE_API_KEY")
AGENT_ID = os.environ.get("SURFACE_TEST_AGENT_ID")


def _client() -> httpx.Client:
    if not SVC_KEY or not AGENT_ID:
        pytest.skip("SURFACE_SERVICE_KEY + SURFACE_TEST_AGENT_ID required")
    return httpx.Client(
        base_url=BASE,
        headers={"X-Service-Key": SVC_KEY, "X-Agent-Id": AGENT_ID},
        timeout=20.0,
    )


# ── case conversion (pure) ────────────────────────────────────────────────

def test_case_upper_lower():
    assert convert_case("Hello World", "upper") == "HELLO WORLD"
    assert convert_case("Hello World", "lower") == "hello world"


def test_case_title():
    assert convert_case("hello world from surface", "title") == "Hello World From Surface"


def test_case_sentence():
    inp = "hello world. how are you? i am fine! you?"
    out = convert_case(inp, "sentence")
    assert out == "Hello world. How are you? I am fine! You?"


def test_case_camel_from_spaces():
    assert convert_case("hello world from surface", "camel") == "helloWorldFromSurface"


def test_case_camel_from_snake():
    assert convert_case("hello_world_from_surface", "camel") == "helloWorldFromSurface"


def test_case_pascal():
    assert convert_case("hello world", "pascal") == "HelloWorld"


def test_case_snake_from_camel():
    assert convert_case("helloWorldFromSurface", "snake") == "hello_world_from_surface"


def test_case_kebab_from_pascal():
    assert convert_case("HelloWorldFromSurface", "kebab") == "hello-world-from-surface"


def test_case_snake_handles_acronyms():
    # XMLHttpRequest is the classic torture test.
    assert convert_case("XMLHttpRequest", "snake") == "xml_http_request"


def test_case_empty_string_returns_empty():
    assert convert_case("", "snake") == ""
    assert convert_case("", "camel") == ""


def test_case_unknown_mode_raises():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        convert_case("x", "klingon")
    assert ei.value.status_code == 400


# ── stats (pure) ──────────────────────────────────────────────────────────

def test_stats_basic():
    s = "Hello world. This is Surface."
    out = compute_stats(s)
    assert out["chars"] == len(s)
    assert out["words"] == 5
    assert out["sentences"] == 2
    assert out["paragraphs"] == 1


def test_stats_empty():
    out = compute_stats("")
    assert out["chars"] == 0
    assert out["words"] == 0
    assert out["sentences"] == 0
    assert out["paragraphs"] == 0
    assert out["reading_time_min"] == 0.0


def test_stats_chars_no_space():
    out = compute_stats("a b c")
    assert out["chars_no_space"] == 3


def test_stats_paragraphs():
    s = "para one.\n\npara two.\n\npara three."
    out = compute_stats(s)
    assert out["paragraphs"] == 3


def test_stats_reading_time_scales():
    long = " ".join(["word"] * 460)  # 460 words @ 230 wpm = 2 min
    out = compute_stats(long)
    assert out["words"] == 460
    assert out["reading_time_min"] == 2.0


def test_stats_unicode_words():
    # Latin-1 unicode words should be detected by \w with re.UNICODE.
    out = compute_stats("café croissant baguette")
    assert out["words"] == 3


# ── strip helpers (pure) ──────────────────────────────────────────────────

def test_strip_html_basic():
    h = "<p>Hello <b>world</b></p><p>Second.</p>"
    out = strip_html(h)
    assert "Hello" in out
    assert "world" in out
    assert "<" not in out
    assert ">" not in out


def test_strip_html_drops_script_style():
    h = "<style>p{color:red}</style><p>keep me</p><script>alert(1)</script>"
    out = strip_html(h)
    assert "keep me" in out
    assert "alert" not in out
    assert "color:red" not in out


def test_strip_html_decodes_entities():
    out = strip_html("Tom &amp; Jerry &lt;3")
    assert "Tom & Jerry <3" in out


def test_strip_markdown_headings_and_emphasis():
    md = "# Title\n\nThis is **bold** and *italic* and `code`."
    out = strip_markdown(md)
    assert "Title" in out
    assert "**" not in out
    assert "*italic*" not in out
    assert "`code`" not in out
    assert "code" in out


def test_strip_markdown_links():
    md = "See [Surface](https://surface.agentnation.in) for more."
    out = strip_markdown(md)
    assert "Surface" in out
    assert "https://" not in out


def test_strip_markdown_lists():
    md = "- one\n- two\n- three"
    out = strip_markdown(md)
    assert "one" in out and "two" in out and "three" in out
    assert "- one" not in out


def test_strip_markdown_code_fences():
    md = "before\n\n```python\ncode here\n```\n\nafter"
    out = strip_markdown(md)
    assert "code here" in out
    assert "```" not in out


# ── flags parsing (pure) ──────────────────────────────────────────────────

def test_parse_flags_none():
    assert _parse_flags(None) == 0
    assert _parse_flags("") == 0


def test_parse_flags_combined():
    import re as _re
    assert _parse_flags("im") == _re.IGNORECASE | _re.MULTILINE
    assert _parse_flags("s") == _re.DOTALL


def test_parse_flags_unknown():
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        _parse_flags("z")


# ── SC pricing matches spec ───────────────────────────────────────────────

def test_sc_costs_match_spec():
    assert SC_COST["case"] == 0.0
    assert SC_COST["stats"] == 0.0
    assert SC_COST["replace"] == 1
    assert SC_COST["regex_test"] == 1
    assert SC_COST["strip"] == 1
    assert SC_COST["diff"] == 1


# ── HTTP integration: free endpoints (no auth) ────────────────────────────

def test_http_health_unauthed():
    r = httpx.get(f"{BASE}/api/v1/commons/text/health", timeout=10.0)
    if r.status_code in (502, 503):
        pytest.skip("surface-api not running")
    assert r.status_code == 200
    j = r.json()
    assert j["service"] == "commons.text"
    assert j["available"]["stdlib"] is True
    assert j["sc_cost"]["case"] == 0
    assert j["sc_cost"]["replace"] == 1


def test_http_case_unauthed():
    r = httpx.post(
        f"{BASE}/api/v1/commons/text/case",
        json={"text": "hello world", "mode": "snake"},
        timeout=10.0,
    )
    if r.status_code in (502, 503):
        pytest.skip("surface-api not running")
    assert r.status_code == 200
    assert r.json()["text"] == "hello_world"


def test_http_stats_unauthed():
    r = httpx.post(
        f"{BASE}/api/v1/commons/text/stats",
        json={"text": "Hello world. Two sentences here."},
        timeout=10.0,
    )
    if r.status_code in (502, 503):
        pytest.skip("surface-api not running")
    assert r.status_code == 200
    j = r.json()
    assert j["words"] == 5
    assert j["sentences"] == 2


# ── HTTP integration: paid endpoints (gated) ──────────────────────────────

def test_http_replace_literal():
    with _client() as c:
        r = c.post(
            "/api/v1/commons/text/replace",
            json={
                "text": "foo bar foo baz foo",
                "pattern": "foo",
                "replacement": "qux",
            },
        )
        if r.status_code in (502, 503):
            pytest.skip("surface-api not running")
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["count"] == 3
        assert j["text"] == "qux bar qux baz qux"


def test_http_replace_regex_with_count_cap():
    with _client() as c:
        r = c.post(
            "/api/v1/commons/text/replace",
            json={
                "text": "a1 a2 a3 a4",
                "pattern": r"a(\d)",
                "replacement": r"b\1",
                "regex": True,
                "count": 2,
            },
        )
        if r.status_code in (502, 503):
            pytest.skip("surface-api not running")
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["count"] == 2
        assert j["text"] == "b1 b2 a3 a4"


def test_http_replace_case_insensitive():
    with _client() as c:
        r = c.post(
            "/api/v1/commons/text/replace",
            json={
                "text": "Foo FOO foo",
                "pattern": "foo",
                "replacement": "X",
                "case_sensitive": False,
            },
        )
        if r.status_code in (502, 503):
            pytest.skip("surface-api not running")
        assert r.status_code == 200, r.text
        assert r.json()["count"] == 3
        assert r.json()["text"] == "X X X"


def test_http_regex_test_groups_and_positions():
    with _client() as c:
        r = c.post(
            "/api/v1/commons/text/regex-test",
            json={
                "pattern": r"(\w+)@(\w+\.\w+)",
                "text": "mail a@b.co and c@d.io and noise",
            },
        )
        if r.status_code in (502, 503):
            pytest.skip("surface-api not running")
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["match_count"] == 2
        m0 = j["matches"][0]
        assert m0["match"] == "a@b.co"
        assert m0["groups"] == ["a", "b.co"]
        assert m0["start"] == 5


def test_http_regex_test_bad_pattern_returns_400():
    with _client() as c:
        r = c.post(
            "/api/v1/commons/text/regex-test",
            json={"pattern": "(unclosed", "text": "x"},
        )
        if r.status_code in (502, 503):
            pytest.skip("surface-api not running")
        assert r.status_code == 400


def test_http_strip_html():
    with _client() as c:
        r = c.post(
            "/api/v1/commons/text/strip",
            json={
                "text": "<p>Hello <b>world</b></p>",
                "mode": "html",
            },
        )
        if r.status_code in (502, 503):
            pytest.skip("surface-api not running")
        assert r.status_code == 200, r.text
        out = r.json()["text"]
        assert "Hello" in out and "world" in out
        assert "<" not in out


def test_http_strip_markdown_both():
    with _client() as c:
        r = c.post(
            "/api/v1/commons/text/strip",
            json={
                "text": "# Title\n\n**bold** and [link](https://x)",
                "mode": "both",
            },
        )
        if r.status_code in (502, 503):
            pytest.skip("surface-api not running")
        assert r.status_code == 200, r.text
        out = r.json()["text"]
        assert "Title" in out and "bold" in out and "link" in out
        assert "**" not in out and "https://" not in out


def test_http_diff_unified():
    with _client() as c:
        r = c.post(
            "/api/v1/commons/text/diff",
            json={
                "a": "alpha\nbeta\ngamma\n",
                "b": "alpha\nBETA\ngamma\ndelta\n",
            },
        )
        if r.status_code in (502, 503):
            pytest.skip("surface-api not running")
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["identical"] is False
        assert j["added"] >= 2  # BETA + delta
        assert j["removed"] >= 1  # beta
        assert "@@" in j["diff"]
        assert "-beta" in j["diff"]
        assert "+BETA" in j["diff"]


def test_http_diff_identical():
    with _client() as c:
        r = c.post(
            "/api/v1/commons/text/diff",
            json={"a": "same\nthing\n", "b": "same\nthing\n"},
        )
        if r.status_code in (502, 503):
            pytest.skip("surface-api not running")
        assert r.status_code == 200, r.text
        j = r.json()
        assert j["identical"] is True
        assert j["added"] == 0 and j["removed"] == 0


def test_http_paid_endpoints_unauthed_rejected():
    r = httpx.post(
        f"{BASE}/api/v1/commons/text/replace",
        json={"text": "x", "pattern": "x", "replacement": "y"},
        timeout=10.0,
    )
    if r.status_code in (502, 503):
        pytest.skip("surface-api not running")
    # Should require auth — 401/403/422 all acceptable depending on stack.
    assert r.status_code in (401, 403, 422)
