"""Tests for the isolated, hard-clamped Claude advisor (agent/advisor.py).

These tests are OFFLINE: no network, no API key, no real anthropic calls.
A FAKE client is injected. They run under the repo's stdlib runner (tests/run.py)
and are also pytest-discoverable (no `import pytest` at module top).
"""

import inspect

from agent.advisor import Advisor


# ---------------------------------------------------------------------------
# Fake Anthropic client plumbing
# ---------------------------------------------------------------------------
class _FakeToolUseBlock:
    type = "tool_use"

    def __init__(self, annotations):
        self.input = {"annotations": annotations}


class _FakeResponse:
    def __init__(self, annotations):
        self.content = [_FakeToolUseBlock(annotations)]


class _FakeMessages:
    def __init__(self, annotations):
        self._annotations = annotations
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse(self._annotations)


class _FakeClient:
    """Returns a fixed set of annotations for any annotate() call."""

    def __init__(self, annotations):
        self.messages = _FakeMessages(annotations)


class _RaisingMessages:
    def create(self, **kwargs):
        raise RuntimeError("boom — simulated API failure")


class _RaisingClient:
    def __init__(self):
        self.messages = _RaisingMessages()


def _candidates():
    return [
        {
            "client_order_id": "A",
            "underlying": "SPY",
            "strategy": "put_credit_spread",
            "rationale": "elevated IV",
            "max_loss": 70.0,
            "est_credit": 30.0,
        },
        {
            "client_order_id": "B",
            "underlying": "QQQ",
            "strategy": "put_credit_spread",
            "rationale": "trend",
            "max_loss": 80.0,
            "est_credit": 25.0,
        },
        {
            "client_order_id": "C",
            "underlying": "IWM",
            "strategy": "put_credit_spread",
            "rationale": "mean reversion",
            "max_loss": 60.0,
            "est_credit": 20.0,
        },
    ]


def _by_id(results):
    return {r["client_order_id"]: r for r in results}


# ---------------------------------------------------------------------------
# Clamping + veto behavior
# ---------------------------------------------------------------------------
def test_overlarge_mult_clamps_to_one():
    # LLM tries to push A's size to 5.0 -> must clamp to 1.0 (can only lower).
    client = _FakeClient([
        {"client_order_id": "A", "keep": True, "allocation_mult": 5.0,
         "thesis": "greedy"},
    ])
    out = _by_id(Advisor(client=client).annotate(_candidates(), None))
    assert out["A"]["allocation_mult"] == 1.0
    assert out["A"]["keep"] is True


def test_negative_mult_clamps_to_zero():
    # -2 -> clamp to 0.0.
    client = _FakeClient([
        {"client_order_id": "B", "keep": True, "allocation_mult": -2,
         "thesis": "negative"},
    ])
    out = _by_id(Advisor(client=client).annotate(_candidates(), None))
    assert out["B"]["allocation_mult"] == 0.0


def test_keep_false_drops_candidate():
    client = _FakeClient([
        {"client_order_id": "C", "keep": False, "allocation_mult": 1.0,
         "thesis": "imminent earnings"},
    ])
    out = _by_id(Advisor(client=client).annotate(_candidates(), None))
    assert out["C"]["keep"] is False
    assert out["C"]["thesis"] == "imminent earnings"


def test_omitted_candidate_kept_unchanged():
    # LLM only returns an opinion on A; B and C are omitted -> pass-through.
    client = _FakeClient([
        {"client_order_id": "A", "keep": False, "allocation_mult": 0.5,
         "thesis": "drop A"},
    ])
    out = _by_id(Advisor(client=client).annotate(_candidates(), None))
    # A reflects the (clamped) opinion.
    assert out["A"]["keep"] is False
    assert out["A"]["allocation_mult"] == 0.5
    # B and C untouched: kept at full size, empty thesis.
    for cid in ("B", "C"):
        assert out[cid]["keep"] is True
        assert out[cid]["allocation_mult"] == 1.0
        assert out[cid]["thesis"] == ""


def test_all_clamps_in_one_response():
    # 5.0 -> 1.0, -2 -> 0.0, keep:false drops, omitted kept unchanged.
    client = _FakeClient([
        {"client_order_id": "A", "keep": True, "allocation_mult": 5.0,
         "thesis": "x"},
        {"client_order_id": "B", "keep": False, "allocation_mult": -2,
         "thesis": "y"},
    ])
    out = _by_id(Advisor(client=client).annotate(_candidates(), None))
    assert out["A"]["allocation_mult"] == 1.0 and out["A"]["keep"] is True
    assert out["B"]["allocation_mult"] == 0.0 and out["B"]["keep"] is False
    assert out["C"]["keep"] is True and out["C"]["allocation_mult"] == 1.0


def test_llm_cannot_add_unknown_candidate():
    # An id never passed in must be ignored — the advisor can't ADD trades.
    client = _FakeClient([
        {"client_order_id": "Z", "keep": True, "allocation_mult": 1.0,
         "thesis": "phantom"},
    ])
    results = Advisor(client=client).annotate(_candidates(), None)
    ids = {r["client_order_id"] for r in results}
    assert ids == {"A", "B", "C"}
    assert len(results) == 3


# ---------------------------------------------------------------------------
# Fail-safe behavior
# ---------------------------------------------------------------------------
def test_raising_client_full_passthrough():
    results = Advisor(client=_RaisingClient()).annotate(_candidates(), None)
    assert len(results) == 3
    for r in results:
        assert r["keep"] is True
        assert r["allocation_mult"] == 1.0
        assert r["thesis"] == ""


def test_missing_client_does_not_crash_on_import():
    # No client injected and no API call made (empty candidates) -> no error,
    # no anthropic import required.
    assert Advisor().annotate([], None) == []


def test_garbage_response_full_passthrough():
    # tool input is not a dict with annotations -> fail safe pass-through.
    class _GarbageMessages:
        def create(self, **kwargs):
            class _Resp:
                content = ["not a block"]
            return _Resp()

    class _GarbageClient:
        messages = _GarbageMessages()

    results = Advisor(client=_GarbageClient()).annotate(_candidates(), None)
    for r in results:
        assert r["keep"] is True and r["allocation_mult"] == 1.0


def test_malformed_annotation_entry_kept_unchanged():
    # A non-dict / mult that isn't a number -> that candidate stays safe.
    client = _FakeClient([
        "not a dict",
        {"client_order_id": "A", "keep": True, "allocation_mult": "oops",
         "thesis": "bad number"},
    ])
    out = _by_id(Advisor(client=client).annotate(_candidates(), None))
    # Bad number clamps to 1.0 (safe), candidate still kept.
    assert out["A"]["allocation_mult"] == 1.0
    assert out["A"]["keep"] is True


# ---------------------------------------------------------------------------
# Isolation invariant: the advisor never imports the trading core.
# ---------------------------------------------------------------------------
def test_advisor_module_is_isolated():
    import agent.advisor as mod

    src = inspect.getsource(mod)
    forbidden = (
        "core.execution", "core.risk", "core.store", "core.brokers",
        "import cli", "from cli",
    )
    for token in forbidden:
        assert token not in src, f"advisor must not reference {token!r}"
