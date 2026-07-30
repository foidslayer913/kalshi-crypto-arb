"""Unit tests for the trades probe's normalisation, which must handle either wire convention."""

import pytest

from scripts.probe_trades import _as_dollars, _as_epoch, _first_present


def test_cents_and_dollars_both_normalise_to_dollars():
    assert _as_dollars(77) == pytest.approx(0.77)        # integer cents
    assert _as_dollars("0.7700") == pytest.approx(0.77)  # dollar string
    assert _as_dollars("77") == pytest.approx(0.77)
    assert _as_dollars(0.99) == pytest.approx(0.99)


def test_a_dollar_priced_contract_is_not_mistaken_for_cents():
    assert _as_dollars(1.0) == pytest.approx(1.0)
    assert _as_dollars("1.0000") == pytest.approx(1.0)


def test_bad_prices_are_rejected():
    assert _as_dollars(None) is None
    assert _as_dollars("not a price") is None


def test_epoch_handles_iso_seconds_and_milliseconds():
    iso = _as_epoch("2026-07-30T00:15:00Z")
    assert iso == pytest.approx(1785370500.0)
    assert _as_epoch(1785370500) == pytest.approx(1785370500.0)
    assert _as_epoch(1785370500000) == pytest.approx(1785370500.0)  # ms detected by magnitude
    assert _as_epoch("nonsense") is None
    assert _as_epoch(None) is None


def test_first_present_skips_missing_and_null_keys():
    assert _first_present({"b": 2}, ("a", "b")) == ("b", 2)
    assert _first_present({"a": None, "b": 5}, ("a", "b")) == ("b", 5)
    assert _first_present({}, ("a",)) == (None, None)
