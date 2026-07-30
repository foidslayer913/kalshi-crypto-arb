"""Preflight check for a machine that is about to run the capture.

Setting this up on a new machine has several independent ways to be subtly wrong — a key path that
does not exist, a Demo key where a Live one is needed, a blocked network — and most of them surface
later as an empty capture rather than an error. That is the expensive failure: a process that looks
healthy and records nothing.

Every check reports pass/fail with the specific fix, and nothing here writes or trades: it reads
configuration and issues GETs.

    python -m scripts.check_setup
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"

results: list[tuple[str, str, str]] = []


def record(status: str, label: str, detail: str = "") -> None:
    results.append((status, label, detail))


def check_python() -> None:
    version = sys.version_info
    if version >= (3, 10):
        record(PASS, "Python version", f"{version.major}.{version.minor}.{version.micro}")
    else:
        record(FAIL, "Python version", f"{version.major}.{version.minor} — need 3.10+")


def check_dependencies() -> None:
    missing = []
    for module in ("httpx", "websockets", "pydantic", "dotenv", "cryptography"):
        try:
            __import__(module)
        except ImportError:
            missing.append(module)
    if missing:
        record(FAIL, "Dependencies", f"missing {', '.join(missing)} — run: pip install -r requirements.txt")
    else:
        record(PASS, "Dependencies", "all importable")


def check_env() -> tuple[str | None, str | None]:
    try:
        from dotenv import load_dotenv
    except ImportError:
        record(FAIL, ".env", "python-dotenv not installed")
        return None, None

    if not Path(".env").exists():
        record(FAIL, ".env file", "not found — copy .env.example to .env and fill it in")
        return None, None
    load_dotenv()

    api_key = os.getenv("KALSHI_LIVE_API_KEY_ID") or os.getenv("KALSHI_API_KEY_ID")
    key_path = os.getenv("KALSHI_LIVE_PRIVATE_KEY_PATH") or os.getenv("KALSHI_PRIVATE_KEY_PATH")
    if not api_key or not key_path:
        record(FAIL, "Credentials in .env", "set KALSHI_LIVE_API_KEY_ID and KALSHI_LIVE_PRIVATE_KEY_PATH")
        return None, None

    which = "KALSHI_LIVE_*" if os.getenv("KALSHI_LIVE_API_KEY_ID") else "KALSHI_* (fallback)"
    record(PASS, "Credentials in .env", f"{which}, key id ...{api_key[-6:]}")
    return api_key, key_path


def check_private_key(key_path: str | None) -> object | None:
    if key_path is None:
        record(FAIL, "Private key", "skipped — no path configured")
        return None
    path = Path(key_path)
    if not path.exists():
        # The most common migration mistake: a Windows path carried over to macOS unchanged.
        hint = " (Windows path on a non-Windows machine?)" if "\\" in key_path else ""
        record(FAIL, "Private key file", f"not found at {key_path}{hint}")
        return None
    try:
        from ingestion.kalshi_auth import load_private_key

        key = load_private_key(path.read_bytes())
    except Exception as error:  # noqa: BLE001 - report any parse failure verbatim
        record(FAIL, "Private key parses", f"{type(error).__name__}: {error}")
        return None
    record(PASS, "Private key parses", f"RSA, {path}")
    return key


def check_kalshi(api_key: str | None, private_key: object | None) -> None:
    """A signed GET is the only check that proves the key matches the environment."""
    if api_key is None or private_key is None:
        record(FAIL, "Kalshi authenticated read", "skipped — credentials unavailable")
        return
    try:
        import httpx

        from ingestion.kalshi_auth import auth_headers

        path = "/trade-api/v2/markets"
        headers = auth_headers(private_key, api_key, "GET", path)
        with httpx.Client(base_url="https://api.elections.kalshi.com", timeout=20.0) as client:
            response = client.get(path, headers=headers, params={"limit": 1, "status": "open"})
        if response.status_code == 200:
            record(PASS, "Kalshi authenticated read", "signed GET against Live succeeded")
        elif response.status_code in (401, 403):
            record(
                FAIL, "Kalshi authenticated read",
                f"HTTP {response.status_code} — a Demo key gets this against Live; generate a key "
                "on the Live account",
            )
        else:
            record(FAIL, "Kalshi authenticated read", f"HTTP {response.status_code}")
    except Exception as error:  # noqa: BLE001
        record(FAIL, "Kalshi authenticated read", f"{type(error).__name__}: {error}")


def check_reachable(label: str, url: str, required: bool) -> None:
    try:
        import httpx

        response = httpx.get(url, timeout=20.0)
        if response.status_code < 400:
            record(PASS, label, "reachable")
        else:
            record(FAIL if required else WARN, label, f"HTTP {response.status_code}")
    except Exception as error:  # noqa: BLE001
        record(FAIL if required else WARN, label, f"{type(error).__name__}")


def check_capture_dir() -> None:
    directory = Path(os.getenv("CAPTURE_DIR", "captures"))
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".write-probe"
        probe.write_text("ok")
        probe.unlink()
    except Exception as error:  # noqa: BLE001
        record(FAIL, "Capture directory writable", f"{directory}: {error}")
        return

    existing = list(directory.glob("capture-*.jsonl")) + list(directory.glob("capture-*.jsonl.gz"))
    free_gb = shutil.disk_usage(directory).free / 1e9
    detail = f"{directory} writable, {len(existing)} existing day file(s), {free_gb:.1f} GB free"
    # ~150 MB/day uncompressed for a busy series; below a few GB a long run will hit the wall.
    record(WARN if free_gb < 5 else PASS, "Capture directory", detail)


def main() -> None:
    print("Preflight check — nothing here trades or writes market data.\n")
    check_python()
    check_dependencies()
    api_key, key_path = check_env()
    private_key = check_private_key(key_path)
    check_kalshi(api_key, private_key)
    check_reachable("Binance reachable (index backfill)", "https://data-api.binance.vision/api/v3/ping", False)
    check_reachable("Coinbase reachable (live index)", "https://api.coinbase.com/v2/time", False)
    check_capture_dir()

    width = max(len(label) for _, label, _ in results)
    for status, label, detail in results:
        print(f"  [{status}] {label:<{width}}  {detail}")

    failures = sum(1 for status, _, _ in results if status == FAIL)
    warnings = sum(1 for status, _, _ in results if status == WARN)
    print()
    if failures:
        print(f"{failures} check(s) failed — fix these before starting the capture.")
        sys.exit(1)
    if warnings:
        print(f"Ready, with {warnings} warning(s). The capture will run; see above for what is degraded.")
    else:
        print("Ready. Start with:")
        print("  python capture_live.py --series KXBTC15M --hours 2 --refresh-minutes 5")


if __name__ == "__main__":
    main()
