"""
THE GO-LIVE GATE — the deterministic, fail-closed path from paper to live.

Cardinal rule (docs/14, docs/05 §1.2): **an LLM must be able to satisfy NONE of
these conditions.** Each is an out-of-band HUMAN action. Code (or the advisor
LLM) can *propose*; only the operator, through these five independent gates, can
*enable live*. Paper stays the default; the deterministic risk gate still binds
in live, and the live size is clamped to the smallest ramp tier.

The five independent conditions — ALL required, together, evaluated AT ORDER /
BUILD TIME (not once at startup, so the gate can't be satisfied and forgotten):

  1. mode:live           — `mode: live` in config/config.yaml. One condition;
                           does nothing on its own.
  2. live_endpoint       — a SEPARATE live credential, different from paper. For
                           IBKR that is the Gateway on a LIVE port (4001 / 7496),
                           distinct from the paper port (4002 / 7497). The adapter
                           refuses to construct a live adapter against a paper
                           port, so "is the live endpoint" is verified, not assumed.
  3. live_enabled_file   — a dated `~/.aistudios/LIVE_ENABLED` whose first line is
                           TODAY's date, written out-of-band by the human. The
                           advisor LLM has no tool to write it.
  4. session_confirmation— a per-session typed phrase, `CONFIRM LIVE <today>`,
                           collected from the operator's interactive terminal by
                           `cli.py go-live` (NOT a CLI argument or model text), and
                           date-bound so a stale "yes" can never be replayed.
  5. ramp_tier           — `go_live.ramp_tier >= 1` selecting a defined ramp tier;
                           live starts at the SMALLEST size and only steps up after
                           a documented number of clean live sessions. The risk
                           gate enforces the tier's notional/position caps as HARD
                           additional limits.

Fail-closed: any missing/unreadable/ambiguous condition → NOT live.

Which gates are TRUE security boundaries vs. UX (be honest — see docs/05 §1.4):
  - Gate 4 (interactive typed, date-bound, read from the operator's TTY) and the
    requirement that it can't come from an argument are the strongest boundary.
  - Gate 3 (out-of-band dated file) and Gate 2 (a live-authenticated Gateway on a
    live port) require real human/broker actions the runtime advisor cannot take.
  - Gates 1 and 5 are config the human owns — intent signals, weaker as STANDALONE
    boundaries. Their value is defense-in-depth: live needs ALL five at once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Optional

# IB live ports (paper is 4002 Gateway / 7497 TWS). A live adapter MUST point at
# one of these and the gate refuses the paper ports for the live endpoint.
LIVE_PORTS = {4001, 7496}
PAPER_PORTS = {4002, 7497}

# Default location of the out-of-band LIVE_ENABLED token (overridable via config
# `go_live.live_enabled_file`, mainly so tests can point at a temp file).
LIVE_ENABLED_PATH = Path.home() / ".aistudios" / "LIVE_ENABLED"

# Persisted store key holding the operator's dated confirmation phrase once
# `cli.py go-live` arms live for the day (so same-day unattended cycles don't
# re-prompt). It goes stale automatically at the date rollover.
ARMED_KV_KEY = "golive_armed"


def confirmation_phrase(today: Optional[date] = None) -> str:
    """The EXACT phrase the operator must type this session. Date-bound: a stale
    confirmation from a prior day can never satisfy gate 4."""
    return f"CONFIRM LIVE {(today or date.today()).isoformat()}"


@dataclass
class GoLiveCondition:
    name: str
    ok: bool
    detail: str


@dataclass
class GoLiveDecision:
    allowed: bool
    conditions: list[GoLiveCondition] = field(default_factory=list)

    @property
    def reasons(self) -> list[str]:
        """One human-readable reason per UNMET condition (why live is blocked)."""
        return [f"{c.name}: {c.detail}" for c in self.conditions if not c.ok]

    @property
    def satisfied(self) -> list[str]:
        return [c.name for c in self.conditions if c.ok]


class GoLiveGate:
    """Evaluates all five conditions. Fail-closed; any missing one → not live."""

    def __init__(self, config: Any, *, live_enabled_path: Optional[Any] = None,
                 today: Optional[date] = None):
        self.config = config
        gl = _golive_cfg(config)
        self.live_enabled_path = Path(
            live_enabled_path or gl.get("live_enabled_file") or LIVE_ENABLED_PATH)
        self.today = today or date.today()

    def evaluate(self, confirmation: Optional[str] = None) -> GoLiveDecision:
        conds = [
            self._check_mode(),
            self._check_live_endpoint(),
            self._check_live_enabled_file(),
            self._check_confirmation(confirmation),
            self._check_ramp_tier(),
        ]
        return GoLiveDecision(allowed=all(c.ok for c in conds), conditions=conds)

    # ── 1. mode:live ──────────────────────────────────────────────────────────
    def _check_mode(self) -> GoLiveCondition:
        mode = str(getattr(self.config, "mode", "paper")).lower()
        ok = mode == "live"
        return GoLiveCondition(
            "mode:live", ok,
            "config mode is 'live'" if ok else
            f"config mode is '{mode}', not 'live' — set `mode: live` in config/config.yaml",
        )

    # ── 2. separate live endpoint (live IB port, not the paper port) ──────────
    def _check_live_endpoint(self) -> GoLiveCondition:
        brokers = getattr(self.config, "brokers", {}) or {}
        exec_broker = str(brokers.get("execution", "")).lower()
        paper_port = _safe_int(brokers.get("ibkr_port", 4002), 4002)
        live_port = _safe_int(brokers.get("ibkr_live_port", 4001))
        is_ibkr = exec_broker in ("ibkr", "ibkr_paper", "ibkr_live")
        if not is_ibkr:
            return GoLiveCondition(
                "live_endpoint", False,
                f"execution broker '{exec_broker or '(unset)'}' is not IBKR — live "
                "is only wired for IBKR (set brokers.execution: ibkr_live)")
        if live_port is None:
            return GoLiveCondition(
                "live_endpoint", False,
                "brokers.ibkr_live_port is missing or not a number — cannot verify a "
                "separate live endpoint")
        if live_port in PAPER_PORTS:
            return GoLiveCondition(
                "live_endpoint", False,
                f"brokers.ibkr_live_port={live_port} is a PAPER port — refusing to "
                "treat the paper endpoint as live")
        if live_port not in LIVE_PORTS:
            return GoLiveCondition(
                "live_endpoint", False,
                f"brokers.ibkr_live_port={live_port} is not a known IB live port "
                "(4001 Gateway / 7496 TWS)")
        if live_port == paper_port:
            return GoLiveCondition(
                "live_endpoint", False,
                f"live port {live_port} equals the paper port — the live credential "
                "must be a SEPARATE endpoint from paper")
        return GoLiveCondition(
            "live_endpoint", True,
            f"separate live IB endpoint configured on port {live_port} "
            f"(paper is {paper_port})")

    # ── 3. dated LIVE_ENABLED file written out-of-band ────────────────────────
    def _check_live_enabled_file(self) -> GoLiveCondition:
        p = self.live_enabled_path
        if not p.exists():
            return GoLiveCondition(
                "live_enabled_file", False,
                f"{p} does not exist — write TODAY's date into it out-of-band")
        try:
            content = p.read_text(encoding="utf-8")
        except Exception as e:  # unreadable -> fail closed
            return GoLiveCondition("live_enabled_file", False, f"could not read {p}: {e}")
        first = (content.splitlines() or [""])[0].strip()
        today_str = self.today.isoformat()
        ok = first == today_str
        return GoLiveCondition(
            "live_enabled_file", ok,
            f"{p} dated today" if ok else
            f"{p} first line is '{first}', expected today's date '{today_str}' "
            "(stale or missing date → blocked)")

    # ── 4. per-session typed confirmation (date-bound) ────────────────────────
    def _check_confirmation(self, confirmation: Optional[str]) -> GoLiveCondition:
        expected = confirmation_phrase(self.today)
        ok = confirmation is not None and confirmation.strip() == expected
        return GoLiveCondition(
            "session_confirmation", ok,
            "operator typed the dated confirmation phrase" if ok else
            f"requires typing EXACTLY: {expected}  (collected from the terminal by "
            "`cli.py go-live`; never an argument or model text)")

    # ── 5. ramp tier active (>=1, defined in the ramp table) ──────────────────
    def _check_ramp_tier(self) -> GoLiveCondition:
        gl = _golive_cfg(self.config)
        tier = _safe_int(gl.get("ramp_tier"), 0)
        valid = {_safe_int(r.get("tier")) for r in _ramp_rows(gl)}
        valid.discard(None)
        if tier < 1:
            return GoLiveCondition(
                "ramp_tier", False,
                f"go_live.ramp_tier={tier} (0 = paper) — set it to 1+ to start the "
                "smallest live ramp tier")
        if tier not in valid:
            return GoLiveCondition(
                "ramp_tier", False,
                f"go_live.ramp_tier={tier} is not a defined tier {sorted(valid)}")
        return GoLiveCondition("ramp_tier", True, f"ramp tier {tier} active")


# ── ramp helpers (shared by the factory, the risk gate, and the CLI) ──────────

def _golive_cfg(config: Any) -> dict:
    return (getattr(config, "raw", {}) or {}).get("go_live", {}) or {}


def _safe_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    """int(value), or `default` on any malformed input — so a config typo (a
    string port, a non-numeric tier) degrades to PAPER rather than crashing the
    gate/factory. Fail-closed: a bad value is never read as a valid live setting."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _ramp_rows(gl: dict) -> list[dict]:
    """The ramp table as dict rows, tolerant of a malformed (non-list) config."""
    ramp = gl.get("ramp")
    if not isinstance(ramp, list):
        return []
    return [r for r in ramp if isinstance(r, dict)]


def current_ramp_tier(config: Any) -> Optional[dict]:
    """The active ramp tier's row ({tier, max_notional_usd, max_positions, ...}),
    or None when on tier 0 (paper) / the tier is undefined / config is malformed.
    The human edits the tier in config; the system reads it and NEVER self-promotes
    (docs/05 §1.3)."""
    gl = _golive_cfg(config)
    tier = _safe_int(gl.get("ramp_tier"), 0)
    if tier is None or tier < 1:
        return None
    for r in _ramp_rows(gl):
        if _safe_int(r.get("tier")) == tier:
            return dict(r)
    return None


def live_ramp_caps(config: Any) -> tuple[Optional[float], Optional[int]]:
    """(max_notional_usd, max_positions) for the active live tier, or (None, None)
    on paper. The risk gate treats a live run with no cap here as fail-closed."""
    r = current_ramp_tier(config)
    if not r:
        return None, None
    notional = r.get("max_notional_usd")
    positions = r.get("max_positions")
    return (float(notional) if notional is not None else None,
            int(positions) if positions is not None else None)


def ramp_advancement_status(tier_cfg: Optional[dict], live_metrics: dict,
                            clean_days: int) -> dict:
    """ADVISORY ONLY — reports whether the live record clears the bar to advance to
    the next ramp tier. It never advances anything: promoting a tier is a manual
    config edit by the human (docs/05 §1.3). A single guardrail breach (any KILL
    event, any reconcile mismatch) is what resets `clean_days` to zero upstream.

    Mirrors the spirit of performance.graduation_status, but on LIVE trades and a
    clean-day counter for the CURRENT tier."""
    note = ("Advisory only — advancing a tier is a manual config edit; the system "
            "never self-promotes. A guardrail breach resets the clean-day counter.")
    if not tier_cfg:
        return {"ready_to_advance": False,
                "reasons": ["not on a live ramp tier (tier 0 = paper)"], "note": note}
    reasons: list[str] = []
    need = int(tier_cfg.get("clean_days_to_advance", 10**9))
    if clean_days < need:
        reasons.append(f"{clean_days}/{need} clean live days at tier {tier_cfg.get('tier')}")
    if int(live_metrics.get("trades", 0)) < 1:
        reasons.append("no closed live trades yet")
    if float(live_metrics.get("expectancy_net", 0.0)) <= 0:
        reasons.append(
            f"live net expectancy ${float(live_metrics.get('expectancy_net', 0.0)):.2f}"
            "/trade is not positive")
    return {"ready_to_advance": not reasons, "reasons": reasons, "note": note}
