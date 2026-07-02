"""importer: header detection, delimiter/encoding tolerance (Excel!),
Dutch decimals, soft per-row errors, idempotent re-import, the rebuild
over the touched range and the 'import' run in the audit trail."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from conftest import RUN_DAY

from compass import importer, queries, store
from compass.models import CostModel, PaymentFee, customer_hash

CSV_ORDERS = (
    "extern_id;channel;ordered_at;gross_eur;units;payment_method;status;refunded_eur;customer_ref\n"
    "1001;shopify;2026-06-15T14:32:00+02:00;29,95;1;iDEAL;paid;0;klant@voorbeeld.nl\n"
    "1002;bol;2026-06-15;59,90;2;;paid;;\n"
    "1003;shopify;2026-06-16;29,95;1;ideal;refunded;29,95;klant@voorbeeld.nl\n"
)


def seed_cost_model(conn) -> None:
    store.add_cost_model(
        conn,
        CostModel(
            valid_from=date(2026, 1, 1),
            cogs_per_unit_cents=650,
            shipping_per_order_cents=440,
            fee_pct=0.015,
            fee_fixed_cents=25,
            payment_fees={"ideal": PaymentFee(pct=0.0, fixed_cents=29)},
            bol_commission_pct=0.124,
            vat_rate=0.09,
        ),
    )


def write_csv(tmp_path, text: str, name: str = "import.csv",
              encoding: str = "utf-8") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding=encoding)
    return path


# ── detect_kind ──────────────────────────────────────────────────────


def test_detect_kind_recognizes_the_three_headers():
    assert importer.detect_kind(
        ["extern_id", "channel", "ordered_at", "gross_eur"]
    ) == "orders"
    assert importer.detect_kind(
        ["day", "campaign_id", "campaign_name", "spend_eur", "clicks"]
    ) == "spend"
    assert importer.detect_kind(["day", "units", "source"]) == "inventory"


def test_detect_kind_ignores_case_whitespace_and_extra_columns():
    assert importer.detect_kind(
        ["Extern_ID", " channel ", "ordered_at", "GROSS_EUR", "notitie"]
    ) == "orders"
    assert importer.detect_kind(["foo", "bar"]) is None


# ── orders: Excel-style file (BOM, ';', Dutch decimals) ──────────────


def test_orders_import_handles_bom_semicolons_and_dutch_decimals(
    compass_db, tmp_path
):
    seed_cost_model(compass_db)
    path = write_csv(tmp_path, CSV_ORDERS, encoding="utf-8-sig")  # writes a BOM

    result = importer.import_file(compass_db, path, today=RUN_DAY)

    assert result.kind == "orders"
    assert result.rows == 3
    assert result.upserted == 3
    assert result.errors == []
    rows = {
        row["extern_id"]: row
        for row in compass_db.execute("SELECT * FROM orders")
    }
    assert rows["1001"]["gross_cents"] == 2995
    assert rows["1001"]["payment_method"] == "ideal"  # normalized lowercase
    assert rows["1002"]["gross_cents"] == 5990
    assert rows["1002"]["units"] == 2
    assert rows["1003"]["status"] == "refunded"
    assert rows["1003"]["refunded_cents"] == 2995


def test_orders_import_hashes_customer_ref_and_never_stores_it_raw(
    compass_db, tmp_path
):
    seed_cost_model(compass_db)
    importer.import_file(
        compass_db, write_csv(tmp_path, CSV_ORDERS), today=RUN_DAY
    )
    row = compass_db.execute(
        "SELECT * FROM orders WHERE extern_id = '1001'"
    ).fetchone()
    assert row["customer_hash"] == customer_hash("klant@voorbeeld.nl")
    for stored in compass_db.execute("SELECT raw_json FROM orders"):
        assert "klant@voorbeeld.nl" not in (stored["raw_json"] or "")
    no_ref = compass_db.execute(
        "SELECT customer_hash FROM orders WHERE extern_id = '1002'"
    ).fetchone()
    assert no_ref["customer_hash"] is None


def test_date_only_ordered_at_becomes_noon_amsterdam(compass_db, tmp_path):
    seed_cost_model(compass_db)
    importer.import_file(
        compass_db, write_csv(tmp_path, CSV_ORDERS), today=RUN_DAY
    )
    row = compass_db.execute(
        "SELECT ordered_at, order_day FROM orders WHERE extern_id = '1002'"
    ).fetchone()
    assert row["ordered_at"] == "2026-06-15T12:00:00+02:00"
    assert row["order_day"] == "2026-06-15"


def test_orders_import_rebuilds_daily_metrics_and_records_an_import_run(
    compass_db, tmp_path
):
    seed_cost_model(compass_db)
    importer.import_file(
        compass_db, write_csv(tmp_path, CSV_ORDERS), today=RUN_DAY
    )
    day = compass_db.execute(
        "SELECT * FROM daily_metrics WHERE day = '2026-06-15'"
    ).fetchone()
    assert day is not None
    assert day["orders_count"] == 2
    assert day["revenue_shopify_cents"] == 2995
    assert day["revenue_bol_cents"] == 5990
    # The fully refunded order on the 16th contributes nothing.
    refund_day = compass_db.execute(
        "SELECT * FROM daily_metrics WHERE day = '2026-06-16'"
    ).fetchone()
    assert refund_day["orders_count"] == 0
    # The rebuild runs through `today`, so later days heal too.
    assert (
        compass_db.execute(
            "SELECT COUNT(*) FROM daily_metrics WHERE day = '2026-07-01'"
        ).fetchone()[0]
        == 1
    )
    run = queries.last_runs(compass_db)[0]
    assert run["kind"] == "import"
    assert run["ok"] == 1
    assert run["orders_upserted"] == 3


def test_importing_the_same_file_twice_is_idempotent(compass_db, tmp_path):
    seed_cost_model(compass_db)
    path = write_csv(tmp_path, CSV_ORDERS)
    importer.import_file(compass_db, path, today=RUN_DAY)
    second = importer.import_file(compass_db, path, today=RUN_DAY)
    assert second.rows == 3
    assert second.upserted == 0, "re-import must create nothing new"
    assert compass_db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 3


def test_minimal_orders_header_gets_sensible_defaults(compass_db, tmp_path):
    seed_cost_model(compass_db)
    path = write_csv(
        tmp_path,
        "extern_id,channel,ordered_at,gross_eur\n1005,shopify,2026-06-20,29.95\n",
    )
    result = importer.import_file(compass_db, path, today=RUN_DAY)
    assert result.errors == []
    row = compass_db.execute("SELECT * FROM orders").fetchone()
    assert row["gross_cents"] == 2995  # dot decimals work too
    assert row["units"] == 1
    assert row["status"] == "paid"
    assert row["refunded_cents"] == 0
    assert row["payment_method"] is None


# ── soft per-row errors ──────────────────────────────────────────────


def test_bad_rows_are_reported_per_line_and_never_abort_the_import(
    compass_db, tmp_path
):
    seed_cost_model(compass_db)
    path = write_csv(
        tmp_path,
        "extern_id;channel;ordered_at;gross_eur\n"
        "2001;shopify;2026-06-10;29,95\n"
        "2002;marktplaats;2026-06-10;29,95\n"     # line 3: unknown channel
        "2003;shopify;2026-06-11;gratis\n"        # line 4: bad amount
        "2004;bol;2026-06-11;29,95\n",
    )
    result = importer.import_file(compass_db, path, today=RUN_DAY)
    assert result.rows == 4
    assert result.upserted == 2
    assert len(result.errors) == 2
    assert result.errors[0].startswith("regel 3: ")
    assert "marktplaats" in result.errors[0]
    assert result.errors[1].startswith("regel 4: ")
    assert "gross_eur" in result.errors[1]
    assert compass_db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 2
    run = queries.last_runs(compass_db)[0]
    assert run["kind"] == "import"
    assert run["ok"] == 0


def test_at_most_twenty_row_errors_are_kept(compass_db, tmp_path):
    seed_cost_model(compass_db)
    bad_rows = "".join(
        f"3{i:03d};niemandsland;2026-06-10;29,95\n" for i in range(25)
    )
    path = write_csv(
        tmp_path, "extern_id;channel;ordered_at;gross_eur\n" + bad_rows
    )
    result = importer.import_file(compass_db, path, today=RUN_DAY)
    assert result.rows == 25
    assert result.upserted == 0
    assert len(result.errors) == 20


# ── spend ────────────────────────────────────────────────────────────


def test_spend_import_with_commas_and_quoted_dutch_decimal(compass_db, tmp_path):
    seed_cost_model(compass_db)
    path = write_csv(
        tmp_path,
        "day,campaign_id,campaign_name,spend_eur,impressions,clicks,"
        "meta_purchases,meta_purchase_value_eur\n"
        '2026-06-15,c-1,Prospecting,250.00,41000,620,8,"239,60"\n'
        "2026-06-16,c-1,Prospecting,199.50,,,,\n",
    )
    result = importer.import_file(compass_db, path, today=RUN_DAY)
    assert result.kind == "spend"
    assert result.rows == 2 and result.upserted == 2 and result.errors == []
    rows = compass_db.execute(
        "SELECT * FROM ad_spend_daily ORDER BY day"
    ).fetchall()
    assert rows[0]["spend_cents"] == 25000
    assert rows[0]["meta_purchase_value_cents"] == 23960
    assert rows[0]["impressions"] == 41000
    assert rows[1]["spend_cents"] == 19950
    assert rows[1]["meta_purchases"] is None
    day = compass_db.execute(
        "SELECT spend_cents FROM daily_metrics WHERE day = '2026-06-15'"
    ).fetchone()
    assert day["spend_cents"] == 25000
    assert queries.last_runs(compass_db)[0]["spend_rows_upserted"] == 2


# ── inventory ────────────────────────────────────────────────────────


def test_inventory_import_defaults_to_manual_and_needs_no_cost_model(
    compass_db, tmp_path
):
    # Deliberately NO cost model: a stock count involves no margins.
    path = write_csv(tmp_path, "day;units\n2026-06-30;1830\n")
    result = importer.import_file(compass_db, path, today=RUN_DAY)
    assert result.kind == "inventory"
    assert result.rows == 1 and result.upserted == 1 and result.errors == []
    inventory = queries.latest_inventory(compass_db)
    assert inventory is not None
    assert inventory.units == 1830
    assert inventory.source == "manual"
    assert inventory.day == date(2026, 6, 30)
    assert queries.last_runs(compass_db)[0]["kind"] == "import"


# ── missing cost model on orders/spend ───────────────────────────────


def test_orders_import_without_cost_model_keeps_data_and_reports_it(
    compass_db, tmp_path
):
    result = importer.import_file(
        compass_db, write_csv(tmp_path, CSV_ORDERS), today=RUN_DAY
    )
    assert result.upserted == 3, "the rows themselves are fine and must be kept"
    assert any(error.startswith("Geen kostenmodel") for error in result.errors)
    assert compass_db.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 3
    assert (
        compass_db.execute("SELECT COUNT(*) FROM daily_metrics").fetchone()[0] == 0
    )
    assert queries.last_runs(compass_db)[0]["ok"] == 0


# ── file-level failures raise ────────────────────────────────────────


def test_unrecognizable_header_raises_a_dutch_valueerror(compass_db, tmp_path):
    path = write_csv(tmp_path, "kolom_a;kolom_b\n1;2\n")
    with pytest.raises(ValueError, match="kolomkoppen"):
        importer.import_file(compass_db, path, today=RUN_DAY)


def test_explicit_kind_with_mismatched_header_raises(compass_db, tmp_path):
    path = write_csv(tmp_path, CSV_ORDERS)
    with pytest.raises(ValueError, match="ontbreken"):
        importer.import_file(compass_db, path, kind="spend", today=RUN_DAY)
    with pytest.raises(ValueError, match="importtype"):
        importer.import_file(compass_db, path, kind="alles", today=RUN_DAY)
