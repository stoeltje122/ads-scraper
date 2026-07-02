"""metrics.py: every formula from the spec, with hand-computed examples.

The reference product: one bag of 60 capsules, €29,95 incl. 9% btw.
2995 / 1.09 = 2747.706… → 2748 cents excl. VAT.
"""

from __future__ import annotations

from datetime import date

import pytest

from compass.metrics import (
    MarginBreakdown,
    ZERO_MARGIN,
    aov_cents,
    blended_cac_cents,
    break_even_roas,
    days_of_stock,
    excl_vat_cents,
    margin_ratio,
    mer,
    meta_roas,
    net_indicative_cents,
    order_margin,
    pct_of_cents,
    prorated_fixed_cents,
    reorder_point_units,
    sellout_day,
    vat_component_cents,
    weighted_daily_sales,
)
from compass.models import (
    CostModel,
    PaymentFee,
    api_amount_to_cents,
    parse_eur_to_cents,
)


def model(**overrides) -> CostModel:
    """The reference cost model used throughout these tests."""
    values = dict(
        valid_from=date(2026, 1, 1),
        cogs_per_unit_cents=650,
        shipping_per_order_cents=440,
        fee_pct=0.015,
        fee_fixed_cents=25,
        payment_fees={"ideal": PaymentFee(pct=0.0, fixed_cents=29)},
        bol_commission_pct=0.124,
        vat_rate=0.09,
        fixed_month_cents=40_000,
    )
    values.update(overrides)
    return CostModel(**values)


# ── VAT ──────────────────────────────────────────────────────────────


def test_excl_vat_9_pct_on_the_reference_price():
    assert excl_vat_cents(2995, 0.09) == 2748
    assert vat_component_cents(2995, 0.09) == 247


def test_excl_vat_21_pct_in_case_the_accountant_disagrees():
    assert excl_vat_cents(2995, 0.21) == 2475
    assert vat_component_cents(2995, 0.21) == 520


def test_excl_vat_zero_amount_and_zero_rate():
    assert excl_vat_cents(0, 0.09) == 0
    assert excl_vat_cents(2995, 0.0) == 2995
    assert vat_component_cents(2995, 0.0) == 0


def test_gross_always_equals_excl_plus_vat_even_on_tiny_amounts():
    for gross in (1, 2, 3, 7, 109, 2995, 123_456_789):
        assert excl_vat_cents(gross, 0.09) + vat_component_cents(gross, 0.09) == gross


def test_pct_of_cents_rounds_half_away_from_zero():
    assert pct_of_cents(50, 0.01) == 1      # 0.5 → 1
    assert pct_of_cents(-50, 0.01) == -1    # -0.5 → -1
    assert pct_of_cents(2995, 0.124) == 371  # 371.38 → 371
    assert pct_of_cents(2995, 0.015) == 45   # 44.925 → 45


# ── money parsing ────────────────────────────────────────────────────


def test_parse_eur_accepts_api_and_dutch_notations():
    assert parse_eur_to_cents("29.95") == 2995
    assert parse_eur_to_cents("29,95") == 2995
    assert parse_eur_to_cents("1.234,56") == 123456
    assert parse_eur_to_cents("€ 29,95") == 2995
    assert parse_eur_to_cents(29.95) == 2995
    assert parse_eur_to_cents(30) == 3000
    assert parse_eur_to_cents("-4,40") == -440
    assert parse_eur_to_cents(-4.40) == -440


def test_parse_eur_dutch_thousands_without_decimals():
    # '1.005' with no comma is Dutch for €1005 — not €1.005.
    assert parse_eur_to_cents("1.005") == 100500
    assert parse_eur_to_cents("1.234.567") == 123456700
    # …except when it can only be a fraction.
    assert parse_eur_to_cents("0.005") == 1


def test_parse_eur_empty_stays_none():
    assert parse_eur_to_cents(None) is None
    assert parse_eur_to_cents("") is None
    assert parse_eur_to_cents("   ") is None


def test_api_amount_is_strict_decimal_never_dutch_thousands():
    assert api_amount_to_cents("123.45") == 12345
    assert api_amount_to_cents("1.005") == 101   # ≠ parse_eur_to_cents!
    assert api_amount_to_cents("1.004") == 100
    assert api_amount_to_cents(29.95) == 2995
    assert api_amount_to_cents("0") == 0
    assert api_amount_to_cents(None) is None
    assert api_amount_to_cents("  ") is None


# ── contribution margin per order ────────────────────────────────────


def test_margin_shopify_ideal_order_reference_case():
    # €29,95 via iDEAL: 2748 excl − 650 COGS − 440 verzend − 29 iDEAL = 1629.
    b = order_margin(
        gross_cents=2995, net_cents=2748, refunded_cents=0, status="paid",
        units=1, channel="shopify", payment_method="ideal", cost_model=model(),
    )
    assert b == MarginBreakdown(2748, 650, 440, 29, 0)
    assert b.margin_cents == 1629
    assert b.ratio == pytest.approx(1629 / 2748)


def test_margin_uses_default_fee_for_unknown_payment_method():
    # Default fee: 1,5% × 2995 = 44.925 → 45, plus 25 vast = 70.
    b = order_margin(
        gross_cents=2995, net_cents=2748, refunded_cents=0, status="paid",
        units=1, channel="shopify", payment_method="creditcard", cost_model=model(),
    )
    assert b.payment_fee_cents == 70
    assert b.margin_cents == 2748 - 650 - 440 - 70


def test_margin_payment_method_lookup_is_case_insensitive():
    b = order_margin(
        gross_cents=2995, net_cents=2748, refunded_cents=0, status="paid",
        units=1, channel="shopify", payment_method="iDEAL", cost_model=model(),
    )
    assert b.payment_fee_cents == 29


def test_margin_bol_order_derives_vat_and_pays_commission_not_fees():
    # bol gives no VAT split: 2995/1.09 → 2748. Commissie 12,4% × 2995 = 371.
    b = order_margin(
        gross_cents=2995, net_cents=None, refunded_cents=0, status="paid",
        units=1, channel="bol", payment_method=None, cost_model=model(),
    )
    assert b == MarginBreakdown(2748, 650, 440, 0, 371)
    assert b.margin_cents == 2748 - 650 - 440 - 371  # 1287


def test_margin_two_units_double_cogs_single_shipping():
    # 2 zakjes: gross 5990, excl 5495 (5990/1.09 = 5495.41), COGS ×2, verzend ×1.
    b = order_margin(
        gross_cents=5990, net_cents=5495, refunded_cents=0, status="paid",
        units=2, channel="shopify", payment_method="ideal", cost_model=model(),
    )
    assert b.cogs_cents == 1300
    assert b.shipping_cents == 440
    assert b.margin_cents == 5495 - 1300 - 440 - 29


def test_margin_fully_refunded_order_contributes_nothing():
    b = order_margin(
        gross_cents=2995, net_cents=2748, refunded_cents=0, status="refunded",
        units=1, channel="shopify", payment_method="ideal", cost_model=model(),
    )
    assert b == ZERO_MARGIN
    assert b.margin_cents == 0
    assert b.ratio is None


def test_margin_refund_of_full_amount_equals_refunded_status():
    b = order_margin(
        gross_cents=2995, net_cents=2748, refunded_cents=2995, status="paid",
        units=1, channel="shopify", payment_method="ideal", cost_model=model(),
    )
    assert b == ZERO_MARGIN


def test_margin_partial_refund_scales_revenue_keeps_unit_costs():
    # €10 terugbetaald: effectief 1995 van 2995. Omzet excl schaalt mee:
    # 2748 × 1995/2995 = 1830.47 → 1830. Kosten blijven staan.
    b = order_margin(
        gross_cents=2995, net_cents=2748, refunded_cents=1000, status="paid",
        units=1, channel="shopify", payment_method="ideal", cost_model=model(),
    )
    assert b.revenue_excl_cents == 1830
    assert b.cogs_cents == 650
    assert b.shipping_cents == 440
    assert b.payment_fee_cents == 29
    assert b.margin_cents == 1830 - 650 - 440 - 29


def test_margin_partial_refund_on_bol_scales_commission_too():
    # Commissie wordt berekend over wat er ná refund overblijft.
    b = order_margin(
        gross_cents=2995, net_cents=None, refunded_cents=1000, status="paid",
        units=1, channel="bol", payment_method=None, cost_model=model(),
    )
    assert b.bol_commission_cents == pct_of_cents(1995, 0.124)  # 247


def test_margin_computed_with_the_cost_model_of_the_order_date():
    # Same order, two model versions → different margins. The caller picks
    # the version by order day; this proves the outcome differs.
    cheap = model(cogs_per_unit_cents=500)
    expensive = model(cogs_per_unit_cents=900, valid_from=date(2026, 6, 1))
    kwargs = dict(
        gross_cents=2995, net_cents=2748, refunded_cents=0, status="paid",
        units=1, channel="shopify", payment_method="ideal",
    )
    assert order_margin(cost_model=cheap, **kwargs).margin_cents == 2748 - 500 - 440 - 29
    assert order_margin(cost_model=expensive, **kwargs).margin_cents == 2748 - 900 - 440 - 29


# ── ratios ───────────────────────────────────────────────────────────


def test_margin_ratio_and_break_even_roas_reference_case():
    ratio = margin_ratio(1629, 2748)
    assert ratio == pytest.approx(0.59279, abs=1e-4)
    assert break_even_roas(ratio) == pytest.approx(2748 / 1629)


def test_break_even_roas_missing_or_nonpositive_margin_has_no_break_even():
    assert break_even_roas(None) is None
    assert break_even_roas(0.0) is None
    assert break_even_roas(-0.2) is None
    assert margin_ratio(1629, 0) is None


def test_mer_is_revenue_excl_over_spend():
    assert mer(274_800, 100_000) == pytest.approx(2.748)
    assert mer(274_800, 0) is None


def test_meta_roas_is_metas_claim_over_spend():
    assert meta_roas(220_000, 100_000) == pytest.approx(2.2)
    assert meta_roas(220_000, 0) is None


def test_blended_cac_divides_spend_over_new_customers():
    assert blended_cac_cents(100_000, 8) == 12_500
    assert blended_cac_cents(100_000, 3) == 33_333  # 33333.33 rounds down
    assert blended_cac_cents(100_000, 0) is None


def test_aov_rounds_half_away():
    assert aov_cents(299_500, 100) == 2995
    assert aov_cents(1001, 2) == 501
    assert aov_cents(1001, 0) is None


# ── indicative monthly P&L ───────────────────────────────────────────


def test_prorated_fixed_costs_partial_and_clamped():
    assert prorated_fixed_cents(40_000, 15, 30) == 20_000
    assert prorated_fixed_cents(40_000, 30, 30) == 40_000
    assert prorated_fixed_cents(40_000, 31, 30) == 40_000
    assert prorated_fixed_cents(40_000, 0, 30) == 0
    assert prorated_fixed_cents(40_000, 10, 0) == 0


def test_net_indicative_is_margin_minus_spend_minus_fixed():
    assert net_indicative_cents(500_000, 200_000, 40_000) == 260_000
    assert net_indicative_cents(100_000, 200_000, 40_000) == -140_000


# ── inventory ────────────────────────────────────────────────────────


def test_weighted_daily_sales_flat_series_equals_plain_average():
    assert weighted_daily_sales([10] * 30) == pytest.approx(10.0)


def test_weighted_daily_sales_weighs_recent_days_heavier():
    # 29 dagen 5/dag, gisteren 20: gewogen 5.97 vs plat gemiddelde 5.5.
    rising = [5] * 29 + [20]
    assert weighted_daily_sales(rising) == pytest.approx((5 * 435 + 20 * 30) / 465)
    assert weighted_daily_sales(rising) > sum(rising) / 30


def test_weighted_daily_sales_empty_window():
    assert weighted_daily_sales([]) == 0.0


def test_days_of_stock_and_edge_cases():
    assert days_of_stock(700, 10.0) == pytest.approx(70.0)
    assert days_of_stock(700, 0.0) is None
    assert days_of_stock(-5, 10.0) == 0.0


def test_reorder_point_is_leadtime_times_rate_times_safety():
    assert reorder_point_units(10.0, 30, 1.3) == pytest.approx(390.0)
    assert reorder_point_units(0.0, 30, 1.3) == 0.0


def test_sellout_day_floors_to_the_last_selling_day():
    assert sellout_day(date(2026, 7, 2), 4.9) == date(2026, 7, 6)
    assert sellout_day(date(2026, 7, 2), None) is None
