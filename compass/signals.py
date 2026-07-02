"""Advisory signal rules: scale up/down ad budget, reorder stock, spend anomaly.

Read-evaluate-write: every rule reads the daily_metrics rollup (and the
inventory/campaign tables) via compass.queries and persists via
compass.store.upsert_signal — this module never invents a formula
(compass.metrics) and never touches an external system. Thresholds live
in THRESHOLD_DEFAULTS; the dashboard overrides them per key through
app_settings rows named "signal.<name>".

Every rule degrades to silence on missing data: a young or empty
database yields no signals, never an exception. Rules evaluate on
rolling 7-day windows over *complete* days (ending yesterday), because
today is always a partial day.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date, timedelta
from typing import Any, Callable

from compass import metrics, queries, store
from compass.models import Signal, fmt_eur

logger = logging.getLogger(__name__)

THRESHOLD_DEFAULTS = {
    "mer_scale_factor": 1.2,     # scale-up: MER ≥ factor × break-even ROAS
    "cac_tolerance": 1.05,       # CAC_7d(today) ≤ tolerance × CAC_7d(a week earlier)
    "spend_anomaly_factor": 2.0, # day spend > factor × trailing-7d average
    "streak_days": 7,            # consecutive days the MER condition must hold
    "min_spend_cents": 2500,     # ignore MER/anomaly rules under €25/day spend
    "signal_cooldown_days": 7,   # at most one signal per type per this many days
}

# The cross-rule: good ad numbers + low stock means the *reorder* signal
# carries the scale-up context instead of a scale-up signal firing.
SUPPRESSED_SCALE_UP_LINE = (
    "De advertentiecijfers zijn goed genoeg om op te schalen, maar bestel "
    "eerst voorraad bij — anders adverteer je straks voor een lege plank."
)

_TYPE_LABELS = {
    "scale_up": "Opschalen",
    "scale_down": "Afschalen",
    "reorder": "Voorraad bestellen",
    "spend_anomaly": "Spend-afwijking",
}


def signal_type_label(type: str) -> str:
    """Dutch label for a signal type (used by web and report)."""
    return _TYPE_LABELS.get(type, type)


def get_thresholds(conn: sqlite3.Connection) -> dict[str, float]:
    """THRESHOLD_DEFAULTS with app_settings overrides ("signal.<name>").

    Overrides accept a Dutch decimal comma; unparseable values fall back
    to the default (a typo in a form must never break signal evaluation).
    """
    values: dict[str, float] = dict(THRESHOLD_DEFAULTS)
    for key, default in THRESHOLD_DEFAULTS.items():
        raw = queries.get_setting(conn, f"signal.{key}")
        if raw is None:
            continue
        try:
            parsed = float(str(raw).strip().replace(",", "."))
        except ValueError:
            logger.warning(
                "Ongeldige drempelwaarde voor signal.%s: %r — standaard %s gebruikt",
                key, raw, default,
            )
            continue
        values[key] = int(parsed) if isinstance(default, int) else parsed
    return values


# ── Dutch number formatting (messages only; money goes through fmt_eur) ──


def _fmt_ratio(value: float, decimals: int = 2) -> str:
    """Fixed decimals with a Dutch comma: 2.408 → '2,41'."""
    return f"{value:.{decimals}f}".replace(".", ",")


def _fmt_num(value: float) -> str:
    """Trim trailing zeros, Dutch comma: 2.0 → '2', 1.3 → '1,3'."""
    return f"{value:g}".replace(".", ",")


def _fmt_day(day: date) -> str:
    return day.strftime("%d-%m-%Y")


# ── rule building blocks ─────────────────────────────────────────────


def _streak_holds(
    conn: sqlite3.Connection,
    today: date,
    streak_days: int,
    min_spend_cents: int,
    condition: Callable[[float, float], bool],
) -> bool:
    """True when the rolling 7-day window ending on each of the last
    `streak_days` complete days has real spend and satisfies `condition`
    on (MER, break-even ROAS)."""
    for offset in range(1, streak_days + 1):
        end = today - timedelta(days=offset)
        window = queries.window_totals(conn, end - timedelta(days=6), end)
        if window.mer is None or window.break_even_roas is None:
            return False
        if window.spend_cents < 7 * min_spend_cents:
            return False
        if not condition(window.mer, window.break_even_roas):
            return False
    return True


def _on_cooldown(
    conn: sqlite3.Connection, signal_type: str, today: date, cooldown_days: int
) -> bool:
    """Any same-type signal (any status) newer than the cooldown window
    blocks a re-fire — founders should not be nagged about the same
    advice every morning."""
    cutoff = today - timedelta(days=cooldown_days)
    row = conn.execute(
        "SELECT 1 FROM signals WHERE type = ? AND day > ? LIMIT 1",
        (signal_type, cutoff.isoformat()),
    ).fetchone()
    return row is not None


def _worst_campaign(
    conn: sqlite3.Connection, today: date, min_spend_cents: int
) -> dict[str, Any] | None:
    """The campaign with the lowest 7-day ROAS-volgens-Meta, ignoring
    campaigns that spent less than the minimum. None when nothing spent
    enough — then the advice stays generic."""
    worst: dict[str, Any] | None = None
    rows = queries.campaign_totals(
        conn, today - timedelta(days=7), today - timedelta(days=1)
    )
    for row in rows:
        if row["spend_cents"] < min_spend_cents:
            continue
        roas = metrics.meta_roas(row["meta_purchase_value_cents"], row["spend_cents"])
        if roas is None:
            continue
        if worst is None or roas < worst["meta_roas"]:
            worst = {
                "name": row["campaign_name"],
                "meta_roas": roas,
                "spend_cents": row["spend_cents"],
            }
    return worst


# ── the rule engine ──────────────────────────────────────────────────


def evaluate(conn: sqlite3.Connection, today: date) -> list[Signal]:
    """Run all four rules for `today` and upsert what fires.

    Returns only NEWLY fired signals (a re-evaluation refreshes the
    numbers of an existing (type, day) row but does not report it
    again). Commits its own writes: store.upsert_signal leaves the
    commit to the caller, and that caller is this function.
    """
    thresholds = get_thresholds(conn)
    streak_days = max(int(thresholds["streak_days"]), 1)
    min_spend_cents = int(thresholds["min_spend_cents"])
    cooldown_days = int(thresholds["signal_cooldown_days"])
    yesterday = today - timedelta(days=1)
    fired: list[Signal] = []

    cost_model = queries.current_cost_model(conn, today)

    # Stock state is shared: it is both the reorder trigger and the
    # scale-up guard (never advise more budget than the shelf can carry).
    inventory = queries.latest_inventory(conn)
    sales_rate = metrics.weighted_daily_sales(
        queries.units_sold_by_day(conn, today - timedelta(days=30), yesterday)
    )
    days_left = (
        metrics.days_of_stock(inventory.units, sales_rate)
        if inventory is not None
        else None
    )
    threshold_days = (
        cost_model.lead_time_days * cost_model.safety_factor
        if cost_model is not None
        else None
    )
    stock_low = (
        days_left is not None
        and threshold_days is not None
        and days_left < threshold_days
    )

    scale_up_suppressed = False
    # Without a cost model there are no margins, no break-even ROAS and
    # no reorder point — only the spend anomaly rule can still work.
    if cost_model is not None:
        # The newest streak window (yesterday−6 .. yesterday) doubles as
        # the "CAC now" window and provides the explanation numbers.
        week = queries.window_totals(conn, today - timedelta(days=7), yesterday)
        prior_week = queries.window_totals(
            conn, today - timedelta(days=14), today - timedelta(days=8)
        )

        # scale_up: MER comfortably above break-even for the whole streak,
        # CAC stable week-over-week, and stock that can absorb more sales.
        mer_scale_factor = thresholds["mer_scale_factor"]
        if _streak_holds(
            conn, today, streak_days, min_spend_cents,
            lambda mer, be_roas: mer >= mer_scale_factor * be_roas,
        ):
            c_now, c_prev = week.cac_cents, prior_week.cac_cents
            cac_stable = (
                c_now is not None
                and c_prev is not None
                and c_now <= thresholds["cac_tolerance"] * c_prev
            )
            if cac_stable:
                if stock_low:
                    scale_up_suppressed = True
                elif not _on_cooldown(conn, "scale_up", today, cooldown_days):
                    signal = Signal(
                        type="scale_up",
                        day=today,
                        message=(
                            "Overweeg het advertentiebudget stapsgewijs "
                            "te verhogen (bijv. +20%)."
                        ),
                        explanation=(
                            f"MER {_fmt_ratio(week.mer)} vs break-even "
                            f"{_fmt_ratio(week.break_even_roas)} over de laatste "
                            f"7 dagen — de MER zit al {streak_days} dagen op rij "
                            f"minstens {_fmt_num(mer_scale_factor)}× boven de "
                            f"break-even ROAS. CAC {fmt_eur(c_now)} nu vs "
                            f"{fmt_eur(c_prev)} een week eerder (stabiel)."
                        ),
                        details={
                            "mer_7d": week.mer,
                            "break_even_roas_7d": week.break_even_roas,
                            "spend_7d_cents": week.spend_cents,
                            "cac_now_cents": c_now,
                            "cac_prev_cents": c_prev,
                            "streak_days": streak_days,
                            "mer_scale_factor": mer_scale_factor,
                            "cac_tolerance": thresholds["cac_tolerance"],
                        },
                    )
                    if store.upsert_signal(conn, signal):
                        fired.append(signal)

        # scale_down: a whole streak of real spend below break-even.
        if _streak_holds(
            conn, today, streak_days, min_spend_cents,
            lambda mer, be_roas: mer < be_roas,
        ) and not _on_cooldown(conn, "scale_down", today, cooldown_days):
            worst = _worst_campaign(conn, today, min_spend_cents)
            explanation = (
                f"MER {_fmt_ratio(week.mer)} vs break-even "
                f"{_fmt_ratio(week.break_even_roas)} over de laatste 7 dagen — "
                f"al {streak_days} dagen op rij onder break-even."
            )
            if worst is not None:
                explanation += (
                    f" Slechtste campagne volgens Meta: '{worst['name']}' "
                    f"(ROAS {_fmt_ratio(worst['meta_roas'])} volgens Meta)."
                )
            signal = Signal(
                type="scale_down",
                day=today,
                message=(
                    "Je verliest geld op advertenties; overweeg het budget "
                    "te verlagen of de slechtste campagne te pauzeren."
                ),
                explanation=explanation,
                details={
                    "mer_7d": week.mer,
                    "break_even_roas_7d": week.break_even_roas,
                    "spend_7d_cents": week.spend_cents,
                    "streak_days": streak_days,
                    "worst_campaign": worst,
                },
            )
            if store.upsert_signal(conn, signal):
                fired.append(signal)

        # reorder: stock runs out within levertijd × veiligheidsfactor.
        if stock_low and not _on_cooldown(conn, "reorder", today, cooldown_days):
            sellout = metrics.sellout_day(today, days_left)
            explanation = (
                f"Voorraad: {inventory.units} stuks (bron: {inventory.source}, "
                f"peildatum {_fmt_day(inventory.day)}). Verkoopsnelheid "
                f"(gewogen over 30 dagen): {_fmt_ratio(sales_rate, 1)} per dag. "
                f"Bestelpunt: {_fmt_num(threshold_days)} dagen voorraad "
                f"(levertijd {cost_model.lead_time_days} dagen × "
                f"veiligheidsfactor {_fmt_num(cost_model.safety_factor)})."
            )
            if scale_up_suppressed:
                explanation += "\n" + SUPPRESSED_SCALE_UP_LINE
            signal = Signal(
                type="reorder",
                day=today,
                message=(
                    f"Bestel nieuwe voorraad; bij de huidige verkoopsnelheid "
                    f"ben je over {int(days_left)} dagen uitverkocht "
                    f"(rond {_fmt_day(sellout)})."
                ),
                explanation=explanation,
                details={
                    "units": inventory.units,
                    "inventory_source": inventory.source,
                    "inventory_day": inventory.day.isoformat(),
                    "sales_rate_per_day": sales_rate,
                    "days_left": days_left,
                    "threshold_days": threshold_days,
                    "lead_time_days": cost_model.lead_time_days,
                    "safety_factor": cost_model.safety_factor,
                    "sellout_day": sellout.isoformat(),
                    "scale_up_suppressed": scale_up_suppressed,
                },
            )
            if store.upsert_signal(conn, signal):
                fired.append(signal)

    # spend_anomaly: yesterday's spend spikes vs the trailing 7-day
    # average. Needs no cost model; UNIQUE(type, day) dedupes naturally.
    anomaly_day = yesterday
    rows = queries.day_rows(conn, anomaly_day - timedelta(days=7), anomaly_day)
    spend_by_day = {row["day"]: row["spend_cents"] for row in rows}
    day_spend = spend_by_day.get(anomaly_day.isoformat())
    history = [
        row["spend_cents"] for row in rows if row["day"] != anomaly_day.isoformat()
    ]
    if day_spend is not None and history:  # no spend history → nothing to compare
        avg_spend = sum(history) / len(history)
        factor = thresholds["spend_anomaly_factor"]
        if day_spend > factor * avg_spend and day_spend >= min_spend_cents:
            avg_display = int(avg_spend + 0.5)
            explanation = (
                f"Spend op {_fmt_day(anomaly_day)}: {fmt_eur(day_spend)}; "
                f"gemiddelde over de 7 dagen ervoor: {fmt_eur(avg_display)}."
            )
            if avg_spend > 0:
                explanation += (
                    f" Dat is {_fmt_ratio(day_spend / avg_spend, 1)}× zo veel."
                )
            signal = Signal(
                type="spend_anomaly",
                day=anomaly_day,
                message=(
                    f"Let op: de spend van gisteren ({fmt_eur(day_spend)}) is "
                    f"meer dan {_fmt_num(factor)}× het 7-daagse gemiddelde "
                    f"({fmt_eur(avg_display)})."
                ),
                explanation=explanation,
                details={
                    "day": anomaly_day.isoformat(),
                    "spend_cents": day_spend,
                    "avg_7d_cents": avg_spend,
                    "spend_anomaly_factor": factor,
                    "history_days": len(history),
                },
            )
            if store.upsert_signal(conn, signal):
                fired.append(signal)

    conn.commit()
    if fired:
        logger.info(
            "Signalen afgegeven op %s: %s",
            today.isoformat(),
            ", ".join(signal_type_label(s.type) for s in fired),
        )
    return fired
