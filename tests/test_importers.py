import pytest

from treasurer.importers import (ImportError_, parse_amount, parse_bank_csv, parse_date,
                                 parse_giving_csv, parse_ofx, decode)
from conftest import sample


@pytest.mark.parametrize("raw,expected", [
    ("$1,234.56", 1234.56), ("(12.00)", -12.0), ("-5", -5.0), ("12.00 CR", 12.0),
    ("40.00 DR", -40.0), ("", None), ("+3.10", 3.1),
])
def test_parse_amount(raw, expected):
    assert parse_amount(raw) == expected


@pytest.mark.parametrize("raw", ["2026-09-01", "09/01/2026", "9/1/26", "Sep 1, 2026",
                                 "2026-09-01T14:22:00Z", "09/01/2026 2:22 PM"])
def test_parse_date(raw):
    assert parse_date(raw) == "2026-09-01"


def test_bank_csv_with_preamble_and_debit_credit_columns():
    rows = parse_bank_csv(decode(sample("bank_statement_sample.csv")))
    assert len(rows) == 24
    publix = rows[1]
    assert publix["date"] == "2026-09-02" and publix["amount"] == -86.42
    assert publix["balance"] == 12413.58
    assert rows[2]["amount"] == 1182.45


def test_bank_csv_single_amount_column_and_duplicates():
    text = "Date,Description,Amount\n2026-09-01,COFFEE,-5.00\n2026-09-01,COFFEE,-5.00\n"
    rows = parse_bank_csv(text)
    assert len(rows) == 2
    assert rows[0]["fingerprint"] != rows[1]["fingerprint"]
    # Same file again gives the same fingerprints, so re-imports are skipped
    assert [r["fingerprint"] for r in parse_bank_csv(text)] == [r["fingerprint"] for r in rows]


def test_bank_csv_unsigned_amount_with_type_column():
    text = "Date,Description,Amount,Type\n09/01/2026,STAPLES,20.00,Debit\n09/02/2026,DEPOSIT,50.00,Credit\n"
    rows = parse_bank_csv(text)
    assert [r["amount"] for r in rows] == [-20.0, 50.0]


def test_bank_csv_rejects_unrelated_file():
    with pytest.raises(ImportError_):
        parse_bank_csv("foo,bar\n1,2\n")


def test_ofx():
    rows = parse_ofx(decode(sample("bank_statement_sample.ofx")))
    assert {r["amount"] for r in rows} == {3.12, -500.0}
    assert all(r["account"] == "3210" for r in rows)
    latest = max(rows, key=lambda r: r["date"])
    assert latest["balance"] == 8203.12


def test_giving_csv_tithely_style():
    rows = parse_giving_csv(decode(sample("giving_sample_tithely.csv")), "Tithe.ly")
    assert len(rows) == 11  # the Failed gift is skipped
    first = rows[0]
    assert first["donor"] == "Jordan Lee" and first["fund"] == "General Fund"
    assert (first["gross"], first["fee"], first["net"]) == (100.0, 3.2, 96.8)


def test_giving_csv_paypal_style_computes_fee():
    text = ('"Date","Time","Name","Type","Status","Gross","Fee","Net"\n'
            '"10/02/2026","10:00:00","Pat Doe","Donation Payment","Completed","50.00","-1.94","48.06"\n')
    rows = parse_giving_csv(text, "PayPal")
    assert rows[0]["fee"] == 1.94 and rows[0]["net"] == 48.06 and rows[0]["fund"] == "General Fund"


def test_giving_csv_without_fee_columns():
    rows = parse_giving_csv("Donation Date,Donor Name,Envelope,Amount\n10/05/2026,Ann,Building,$25\n")
    assert rows[0]["net"] == 25.0 and rows[0]["fee"] == 0 and rows[0]["fund"] == "Building"
