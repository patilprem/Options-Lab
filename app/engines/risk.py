"""
Risk Panel (M7)  — mandatory before live
========================================
Portfolio-level guardrails, evaluated every bar by the paper engine:

  * portfolio max daily loss   -> auto-pause ALL strategies when breached
  * per-strategy daily loss cap -> auto-pause that strategy
  * exposure grouped by underlying and expiry (from open positions)
  * margin utilization vs total allocated capital

Thresholds live in registry.settings (₹; 0 = disabled):
  risk_max_daily_loss        portfolio-wide
  risk_default_loss_cap      default per-strategy cap
  risk_loss_cap:<sid>        per-strategy override

`evaluate()` is a PURE decision function (no side effects) so it is easy to
test; PaperRunner applies the decisions (pausing + events).
"""

from __future__ import annotations

from app.core import registry


def _num(key: str, default: float = 0.0) -> float:
    try:
        v = float(registry.setting(key, ""))
    except (ValueError, TypeError):
        return default
    return v


def portfolio_max_loss() -> float:
    return _num("risk_max_daily_loss")


def loss_cap_for(sid: str) -> float:
    """Per-strategy daily loss cap: override if set, else the global default."""
    override = _num(f"risk_loss_cap:{sid}")
    return override if override > 0 else _num("risk_default_loss_cap")


def evaluate(contexts: dict) -> dict:
    """Decide which strategies breach their caps. Pure — returns decisions,
    mutates nothing. `contexts`: {sid: PaperContext}."""
    total = round(sum(c.day_pnl for c in contexts.values()), 2)
    max_loss = portfolio_max_loss()
    breaches = []
    for sid, c in contexts.items():
        cap = loss_cap_for(sid)
        if cap > 0 and c.day_pnl <= -cap:
            breaches.append({"sid": sid, "day_pnl": round(c.day_pnl, 2), "cap": cap})
    return {
        "portfolio_day_pnl": total,
        "max_loss": max_loss,
        "portfolio_breach": max_loss > 0 and total <= -max_loss,
        "strategy_breaches": breaches,
    }


def exposure(contexts: dict) -> dict:
    """Open-position exposure grouped by underlying and by expiry.
    `premium` = Σ|qty|·mtm (option premium at risk/held)."""
    by_u: dict[str, dict] = {}
    by_e: dict[str, dict] = {}

    def _add(bucket, key):
        return bucket.setdefault(key, {"positions": 0, "net_qty": 0, "premium": 0.0})

    for c in contexts.values():
        for p in c.positions:
            prem = abs(p.qty) * (p.mtm_price or 0.0)
            u = _add(by_u, p.underlying)
            u["positions"] += 1; u["net_qty"] += p.qty; u["premium"] += prem
            ek = p.expiry.isoformat() if p.expiry else "unknown"
            e = _add(by_e, ek)
            e["positions"] += 1; e["net_qty"] += p.qty; e["premium"] += prem

    def _rows(bucket, name):
        return [{name: k, **{**v, "premium": round(v["premium"], 2)}}
                for k, v in sorted(bucket.items())]

    return {"by_underlying": _rows(by_u, "underlying"),
            "by_expiry": _rows(by_e, "expiry")}


def snapshot(contexts: dict) -> dict:
    """Full risk view for the API/UI."""
    ev = evaluate(contexts)
    margin_used = round(sum(getattr(c, "_margin_used", 0.0) for c in contexts.values()), 2)

    # Allocated capital across strategies currently loaded in the paper engine.
    allocated = 0.0
    strategies = []
    for sid, c in contexts.items():
        rec = c.rec
        cap = loss_cap_for(sid)
        allocated += rec.allocated_capital
        strategies.append({
            "id": sid, "name": rec.name, "state": rec.state.value,
            "day_pnl": round(c.day_pnl, 2), "margin_used": round(c._margin_used, 2),
            "loss_cap": cap,
            "cap_breached": cap > 0 and c.day_pnl <= -cap,
            "paused": c.paused,
        })

    exp = exposure(contexts)
    return {
        "portfolio": {
            "allocated": round(allocated, 2),
            "margin_used": margin_used,
            "margin_util_pct": round(margin_used / allocated * 100, 1) if allocated else 0.0,
            "day_pnl": ev["portfolio_day_pnl"],
            "max_daily_loss": ev["max_loss"],
            "breached": ev["portfolio_breach"],
            "loss_used_pct": (round(-ev["portfolio_day_pnl"] / ev["max_loss"] * 100, 1)
                              if ev["max_loss"] and ev["portfolio_day_pnl"] < 0 else 0.0),
        },
        "strategies": strategies,
        "exposure_by_underlying": exp["by_underlying"],
        "exposure_by_expiry": exp["by_expiry"],
        "settings": {
            "max_daily_loss": portfolio_max_loss(),
            "default_loss_cap": _num("risk_default_loss_cap"),
        },
    }
