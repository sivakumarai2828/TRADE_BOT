"""Parsing the model's reply.

Every case here has been seen in production or in a live model evaluation,
including the empty response that silently killed three days of trading on
2026-08-26 and the trailing-prose failure found on 2026-09-01.
"""
from __future__ import annotations

import json

import pytest

from botv2.ai_pm import _extract_json

GOOD = '{"market_view": "ok", "actions": [], "watch_next": []}'


def test_plain_json():
    assert _extract_json(GOOD)["market_view"] == "ok"


def test_json_in_fenced_block():
    assert _extract_json("```json\n" + GOOD + "\n```")["market_view"] == "ok"


def test_json_in_bare_fence():
    assert _extract_json("```\n" + GOOD + "\n```")["market_view"] == "ok"


def test_prose_before_and_after():
    text = "Here is my plan.\n" + GOOD + "\nLet me know if you want changes."
    assert _extract_json(text)["market_view"] == "ok"


def test_leading_whitespace():
    assert _extract_json("\n\n   " + GOOD)["market_view"] == "ok"


def test_nested_braces_span_correctly():
    text = ('{"market_view":"v","actions":[{"action":"BUY","symbol":"X",'
            '"stop":1,"target":5}],"watch_next":[]}')
    assert _extract_json(text)["actions"][0]["symbol"] == "X"


def test_empty_response_raises():
    """The 2026-08-26 failure: reasoning consumed the whole token budget."""
    with pytest.raises(ValueError, match="no JSON"):
        _extract_json("")


def test_no_braces_raises():
    with pytest.raises(ValueError, match="no JSON"):
        _extract_json("I could not produce a plan today.")


def test_unclosed_object_raises():
    with pytest.raises(ValueError, match="no JSON"):
        _extract_json('{"market_view": "cut off", "actions": [')


def test_malformed_but_braced_json_raises_decode_error():
    with pytest.raises(json.JSONDecodeError):
        _extract_json('{"market_view": "x", "actions": [}')


def test_actions_preserved_verbatim():
    text = ('{"market_view":"v","actions":['
            '{"action":"BUY","symbol":"AAPL","stop":300.0,"target":336.0},'
            '{"action":"SELL","symbol":"TSM","reason":"broke"}],"watch_next":["V"]}')
    d = _extract_json(text)
    assert [a["action"] for a in d["actions"]] == ["BUY", "SELL"]
    assert d["watch_next"] == ["V"]


# ── trailing-content cases, found by the 2026-09-01 model evaluation ──
# Slicing from the first "{" to the LAST "}" over-spans whenever a model adds
# a remark after the JSON. Sonnet 5 did exactly that in 1 of 3 trials, which
# would be a 33% cycle-failure rate in production.

def test_trailing_prose_containing_a_brace():
    text = GOOD + "\n\nLet me know if you want changes {see above}."
    assert _extract_json(text)["market_view"] == "ok"


def test_trailing_second_json_object_is_ignored():
    text = GOOD + '\n\n{"note": "ignore me"}'
    assert _extract_json(text)["market_view"] == "ok"


def test_brace_inside_a_string_value():
    text = '{"market_view":"a } brace in text","actions":[],"watch_next":[]}'
    assert _extract_json(text)["market_view"] == "a } brace in text"


def test_escaped_quote_inside_string():
    text = json.dumps({"market_view": 'he said "buy" today',
                       "actions": [], "watch_next": []}) + " trailing words"
    assert _extract_json(text)["market_view"] == 'he said "buy" today'


def test_nested_objects_span_fully_despite_trailing_text():
    text = ('{"market_view":"v","actions":[{"action":"BUY","symbol":"X",'
            '"stop":1,"target":5}],"watch_next":[]} trailing words')
    assert _extract_json(text)["actions"][0]["symbol"] == "X"


def test_fenced_json_with_text_after_the_fence():
    text = "```json\n" + GOOD + "\n```\nHope that helps!"
    assert _extract_json(text)["market_view"] == "ok"
