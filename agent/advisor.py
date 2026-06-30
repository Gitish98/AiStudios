"""Claude reasoning layer — an ISOLATED, hard-clamped advisory module.

SAFETY MODEL (this is the whole point of this module):

  * READ-ONLY / VETO-ONLY. The advisor has NO access to a broker, the store,
    the risk gate, or order placement. It NEVER imports core/execution,
    core/risk, core/store, core/brokers, or cli. It can ONLY annotate.

  * HARD CLAMP. Every value the LLM returns is clamped in deterministic Python
    AFTER the call. `allocation_mult` is clamped to [0.0, 1.0] — the LLM can
    only ever LOWER size, never raise it above 1.0. `keep` defaults to True and
    the LLM may only flip it to False (drop a candidate); it can never ADD a
    candidate that was not passed in. Any candidate the LLM omits or returns
    malformed for is kept unchanged (keep=True, allocation_mult=1.0).

  * FAIL SAFE. If the client is missing / raises / returns garbage, EVERY
    candidate is returned unchanged (keep=True, mult=1.0, thesis=""). The
    deterministic risk gate has already approved these candidates, and the
    advisor only ever reduces exposure, so "no opinion" == pass-through == safe.

The advisor uses Anthropic prompt caching (static system prompt in a cached
system block) and a structured-output tool so the model returns JSON we parse
rather than prose we regex.
"""

from __future__ import annotations

from typing import Any

MODEL = "claude-opus-4-8"

# ---------------------------------------------------------------------------
# Static, cacheable system prompt. Kept BYTE-STABLE so prompt caching works:
# no timestamps, no per-request IDs, no varying content interpolated here.
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "You are a conservative risk advisor for an options trading system. Each "
    "candidate trade you are shown has ALREADY been approved by a deterministic "
    "risk gate. Your role is strictly advisory and VETO-ONLY: you may keep a "
    "candidate as-is, reduce its position size, or drop it entirely.\n\n"
    "You CANNOT add new trades, raise position size above the approved level, "
    "or place orders. You have no access to any broker or account. You only "
    "judge the candidates presented.\n\n"
    "For EACH candidate, decide:\n"
    "  - keep: true to allow the trade, false to drop it. Default to true; only "
    "drop a candidate when the research brief gives a clear, specific reason "
    "(e.g. an imminent catalyst, a thesis that directly contradicts the trade).\n"
    "  - allocation_mult: a multiplier in [0.0, 1.0] applied to the approved "
    "size. 1.0 keeps full size; lower values reduce exposure. You can NEVER "
    "increase size. Use 1.0 unless you have a concrete reason to trim.\n"
    "  - thesis: one short sentence explaining your judgment.\n\n"
    "Be conservative and specific. Reference each candidate by its exact "
    "client_order_id. Respond ONLY by calling the annotate_candidates tool."
)

# Structured-output tool. The model returns JSON we parse from tool input.
ANNOTATE_TOOL = {
    "name": "annotate_candidates",
    "description": (
        "Return your per-candidate judgment. Provide one annotation object per "
        "candidate, referencing each by its exact client_order_id."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "annotations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "client_order_id": {"type": "string"},
                        "keep": {"type": "boolean"},
                        "allocation_mult": {"type": "number"},
                        "thesis": {"type": "string"},
                    },
                    "required": ["client_order_id", "keep", "allocation_mult"],
                },
            }
        },
        "required": ["annotations"],
    },
}


def _clamp_mult(value: Any) -> float:
    """Hard clamp the LLM's multiplier to [0.0, 1.0]. Garbage -> 1.0 (safe)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 1.0
    if v != v:  # NaN
        return 1.0
    return max(0.0, min(1.0, v))


def _passthrough(candidate: dict) -> dict:
    """An unchanged annotation: keep the candidate at full approved size."""
    return {
        "client_order_id": candidate.get("client_order_id"),
        "keep": True,
        "allocation_mult": 1.0,
        "thesis": "",
    }


class Advisor:
    """Read-only, veto-only Claude reasoning layer over approved candidates."""

    def __init__(self, client: Any = None, model: str = MODEL) -> None:
        # `client` is injectable so tests can pass a fake. The real client is
        # constructed lazily on first use so importing this module never
        # requires the anthropic package or an API key.
        self._client = client
        self.model = model

    def _get_client(self) -> Any:
        if self._client is None:
            import anthropic  # lazy: only needed for real calls

            self._client = anthropic.Anthropic()
        return self._client

    def annotate(
        self, candidates: list[dict], research: dict | None
    ) -> list[dict]:
        """Annotate each candidate with the LLM's veto-only judgment.

        Returns one annotation dict per input candidate, in input order:
            {client_order_id, keep, allocation_mult, thesis}

        FAIL SAFE: on any error, returns full pass-through for every candidate.
        The LLM can only lower allocation_mult (clamped to [0,1]) and flip keep
        to False — it can never raise size or add a candidate.
        """
        if not candidates:
            return []

        # Default: every candidate passes through unchanged. We overlay the
        # LLM's opinions on top of this, so anything it omits stays safe.
        result: list[dict] = [_passthrough(c) for c in candidates]
        index_by_id: dict[Any, int] = {}
        for i, c in enumerate(candidates):
            coid = c.get("client_order_id")
            if coid is not None and coid not in index_by_id:
                index_by_id[coid] = i

        annotations = self._call_llm(candidates, research)
        if not annotations:
            return result  # fail safe: no usable opinion -> pass-through

        for ann in annotations:
            if not isinstance(ann, dict):
                continue
            coid = ann.get("client_order_id")
            idx = index_by_id.get(coid)
            if idx is None:
                # LLM referenced an id we never passed in -> ignore. It can
                # never ADD a candidate.
                continue

            # keep defaults True; the LLM may only flip it to False.
            keep = result[idx]["keep"]
            if ann.get("keep") is False:
                keep = False

            mult = _clamp_mult(ann.get("allocation_mult", 1.0))

            thesis = ann.get("thesis", "")
            if not isinstance(thesis, str):
                thesis = ""

            result[idx] = {
                "client_order_id": coid,
                "keep": keep,
                "allocation_mult": mult,
                "thesis": thesis,
            }

        return result

    # -- internal -----------------------------------------------------------

    def _call_llm(
        self, candidates: list[dict], research: dict | None
    ) -> list[dict] | None:
        """Call the LLM and extract the raw annotations list.

        Returns None on ANY failure (missing client, exception, malformed
        response) so the caller falls back to full pass-through.
        """
        try:
            client = self._get_client()
            user_text = self._build_user_message(candidates, research)
            response = client.messages.create(
                model=self.model,
                max_tokens=4096,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                tools=[ANNOTATE_TOOL],
                tool_choice={"type": "tool", "name": "annotate_candidates"},
                messages=[{"role": "user", "content": user_text}],
            )
            return self._extract_annotations(response)
        except Exception:
            # FAIL SAFE — any error means "no opinion" -> pass-through upstream.
            return None

    @staticmethod
    def _build_user_message(
        candidates: list[dict], research: dict | None
    ) -> str:
        import json

        payload = {
            "research_brief": research or {},
            "candidates": [
                {
                    "client_order_id": c.get("client_order_id"),
                    "underlying": c.get("underlying"),
                    "strategy": c.get("strategy"),
                    "rationale": c.get("rationale"),
                    "max_loss": c.get("max_loss"),
                    "est_credit": c.get("est_credit"),
                }
                for c in candidates
            ],
        }
        return (
            "Judge each candidate below. Return one annotation per candidate "
            "via the annotate_candidates tool.\n\n"
            + json.dumps(payload, default=str, sort_keys=True)
        )

    @staticmethod
    def _extract_annotations(response: Any) -> list[dict] | None:
        """Pull the annotations list out of the tool_use block."""
        content = getattr(response, "content", None)
        if content is None:
            return None
        for block in content:
            btype = getattr(block, "type", None)
            if btype is None and isinstance(block, dict):
                btype = block.get("type")
            if btype != "tool_use":
                continue
            tool_input = getattr(block, "input", None)
            if tool_input is None and isinstance(block, dict):
                tool_input = block.get("input")
            if not isinstance(tool_input, dict):
                return None
            anns = tool_input.get("annotations")
            if isinstance(anns, list):
                return anns
            return None
        return None
