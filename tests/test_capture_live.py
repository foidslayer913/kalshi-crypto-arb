"""Tests for the continuous-capture entry point.

Two failures here are silent and expensive: discovery that skips a whole series because of a strike
type it does not recognise, and a subscription that never refreshes while the series rolls over.
Both produce a capture that looks healthy and contains nothing useful.
"""

import capture_live
from capture_live import _market_info_from_dict


def _raw(strike_type, **overrides):
    raw = {
        "ticker": "KXBTC15M-26JUL300015-15",
        "strike_type": strike_type,
        "floor_strike": 64082.64,
        "close_time": "2026-07-30T00:15:00Z",
    }
    raw.update(overrides)
    return raw


def test_greater_or_equal_is_discovered():
    # The 15-minute up/down series uses this; a greater/less-only check finds zero markets for it.
    info = _market_info_from_dict(_raw("greater_or_equal"))
    assert info is not None
    assert info.strike_price == 64082.64
    assert info.ticker == "KXBTC15M-26JUL300015-15"


def test_less_or_equal_uses_the_cap_strike():
    info = _market_info_from_dict(
        _raw("less_or_equal", floor_strike=None, cap_strike=64000.0)
    )
    assert info is not None
    assert info.strike_price == 64000.0


def test_plain_greater_and_less_still_work():
    assert _market_info_from_dict(_raw("greater")) is not None
    assert _market_info_from_dict(_raw("less", floor_strike=None, cap_strike=1.0)) is not None


def test_unsupported_strike_types_are_skipped_not_fatal():
    # Discovery sweeps many strikes; an unsupported one is skipped rather than raising.
    assert _market_info_from_dict(_raw("between")) is None
    assert _market_info_from_dict(_raw("greater", floor_strike=None)) is None
    assert _market_info_from_dict(_raw("greater", close_time=None)) is None


def test_capture_process_imports_no_execution_code():
    """The read-only guarantee is structural, so verify it structurally.

    Walks capture_live's transitive import graph in the source rather than inspecting sys.modules,
    which other tests populate with execution/ imports of their own — that version of this check
    passed alone and failed in a full run, which is worse than no check at all.
    """
    import ast
    from pathlib import Path

    def module_path(dotted: str) -> Path | None:
        candidate = Path(*dotted.split(".")).with_suffix(".py")
        return candidate if candidate.exists() else None

    seen: set[str] = set()
    pending = ["capture_live.py"]
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        tree = ast.parse(Path(current).read_text())
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module)
            elif isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            for name in names:
                path = module_path(name)
                if path is not None:
                    pending.append(str(path))

    offenders = sorted(name for name in seen if name.startswith("execution"))
    assert offenders == [], f"capture must not reach order-placing code, found {offenders}"
