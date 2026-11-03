import pytest

from costlens.money import MoneyError, nanos_to_cents_str, parse_usd_to_nanos, pct_change, share_pct


def test_parse_exact_and_half_even_at_nano():
    assert parse_usd_to_nanos("1") == 1_000_000_000
    assert parse_usd_to_nanos("0.0000000015") == 2  # half-even: 1.5 nanos -> 2
    assert parse_usd_to_nanos("0.0000000025") == 2  # half-even: 2.5 nanos -> 2
    assert parse_usd_to_nanos("-0.0000000035") == -4
    assert parse_usd_to_nanos("123.4567891234") == 123_456_789_123


def test_sum_has_no_float_drift():
    total = sum(parse_usd_to_nanos("0.1") for _ in range(10))
    assert total == parse_usd_to_nanos("1.0")


@pytest.mark.parametrize("bad", ["", "abc", "NaN", "Infinity", "1e30", "  "])
def test_parse_rejects_bad_values(bad):
    with pytest.raises(MoneyError):
        parse_usd_to_nanos(bad)


def test_empty_allowed_when_requested():
    assert parse_usd_to_nanos("", allow_empty=True) == 0


def test_cents_display_half_even():
    assert nanos_to_cents_str(12_345_000_000) == "12.34"  # 12.345 -> half-even -> 12.34
    assert nanos_to_cents_str(12_355_000_000) == "12.36"
    assert nanos_to_cents_str(-1_005_000_000) == "-1.00"


def test_pct_and_share_edges():
    assert pct_change(150, 100) == "50.0"
    assert pct_change(50, 100) == "-50.0"
    assert pct_change(10, 0) is None
    assert pct_change(-50, -100) == "50.0"  # credits: relative to absolute base
    assert share_pct(1, 3) == "33.3"
    assert share_pct(5, 0) is None
