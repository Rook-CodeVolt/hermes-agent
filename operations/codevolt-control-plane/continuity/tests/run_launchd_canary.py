"""Exercise the frozen guard in a separately labelled isolated launchd canary."""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

INTERVAL_SECONDS = 60
RUN_AT_LOAD_TIMEOUT_SECONDS = 20
INTERVAL_TIMEOUT_SECONDS = 100


def build_plist(label: str, python: Path, worker: Path, source: Path, root: Path) -> dict[str, Any]:
    return {
        "Label": label,
        "ProgramArguments": [str(python), str(worker), str(source), str(root)],
        "RunAtLoad": True,
        "StartInterval": INTERVAL_SECONDS,
        "ProcessType": "Background",
        "StandardOutPath": str(root / "launchd.stdout.log"),
        "StandardErrorPath": str(root / "launchd.stderr.log"),
        "EnvironmentVariables": {
            "HOME": str(root),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        },
    }


def invoke(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def is_label_absent(result: subprocess.CompletedProcess[str]) -> bool:
    """Return true only for launchctl's explicit service-not-found result."""
    return result.returncode == 113


def bootstrap_with_bounded_retry(
    domain: str,
    service: str,
    plist_path: Path,
) -> tuple[subprocess.CompletedProcess[str], list[dict[str, Any]], bool]:
    """Retry one transient rc=5 only after proving the unique label absent.

    A non-zero bootstrap can theoretically leave a submitted service behind.
    That state is never retried: the caller is told to boot it out and fail.
    """
    attempts: list[dict[str, Any]] = []
    for attempt_number in range(2):
        result = invoke("/bin/launchctl", "bootstrap", domain, str(plist_path))
        evidence: dict[str, Any] = {
            "attempt": attempt_number + 1,
            "rc": result.returncode,
            "stderr": result.stderr.strip()[:500],
        }
        attempts.append(evidence)
        if result.returncode == 0:
            return result, attempts, False

        loaded = invoke("/bin/launchctl", "print", service)
        evidence["post_failure_print_rc"] = loaded.returncode
        if loaded.returncode == 0:
            return result, attempts, True
        if not is_label_absent(loaded):
            return result, attempts, False
        if result.returncode != 5 or attempt_number == 1:
            return result, attempts, False
        time.sleep(1.0)
    raise AssertionError("bounded bootstrap loop exhausted")


def read_checked_at(state_file: Path) -> int | None:
    try:
        value = json.loads(state_file.read_text(encoding="utf-8")).get("checked_at")
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def wait_for_advance(state_file: Path, prior: int | None, timeout: int) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        checked_at = read_checked_at(state_file)
        if checked_at is not None and (prior is None or checked_at > prior):
            return checked_at
        time.sleep(0.25)
    raise TimeoutError(f"state did not advance within {timeout}s")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the isolated launchd canary and emit a JSON receipt.")
    parser.add_argument("--receipt", type=Path, help="optional durable receipt path")
    parser.add_argument("--keep-fixture", action="store_true", help="preserve the isolated fixture for diagnosis")
    args = parser.parse_args()

    source = Path(__file__).resolve().parents[1] / "codevolt_continuity_guard.py"
    worker = Path(__file__).resolve().with_name("launchd_canary_worker.py")
    repo = source.parents[1]
    root = Path(tempfile.mkdtemp(prefix=".launchd-canary-", dir=repo))
    label = f"com.codevolt.continuity-guard.canary.{os.getpid()}.{int(time.time())}"
    domain = f"gui/{os.getuid()}"
    service = f"{domain}/{label}"
    plist_path = root / f"{label}.plist"
    state_file = root / "state.json"
    receipt: dict[str, Any] = {
        "label": label,
        "fixture": "synthetic-three-board-complete",
        "run_at_load": True,
        "interval_seconds": INTERVAL_SECONDS,
        "bootstrap_rc": None,
        "loaded_print_rc": None,
        "bootout_rc": None,
        "absent_print_rc": None,
        "absence_proven": False,
    }
    bootstrapped = False
    exit_code = 1
    started = time.monotonic()
    try:
        # launchd receives the stable OS interpreter path, not the invoking
        # shell's CommandLineTools shim or virtualenv path.
        plist_path.write_bytes(plistlib.dumps(build_plist(label, Path("/usr/bin/python3"), worker, source, root)))
        bootstrap, attempts, unexpectedly_loaded = bootstrap_with_bounded_retry(domain, service, plist_path)
        receipt["bootstrap_attempts"] = attempts
        receipt["bootstrap_rc"] = bootstrap.returncode
        bootstrapped = bootstrap.returncode == 0 or unexpectedly_loaded
        if bootstrap.returncode != 0:
            receipt["error"] = "bootstrap_failed"
            receipt["bootstrap_stderr"] = bootstrap.stderr.strip()[:500]
            raise RuntimeError("launchd bootstrap failed")

        first = wait_for_advance(state_file, None, RUN_AT_LOAD_TIMEOUT_SECONDS)
        receipt["run_at_load_checked_at"] = first
        loaded = invoke("/bin/launchctl", "print", service)
        receipt["loaded_print_rc"] = loaded.returncode
        if loaded.returncode != 0:
            raise RuntimeError("launchd service absent after RunAtLoad")

        second = wait_for_advance(state_file, first, INTERVAL_TIMEOUT_SECONDS)
        receipt["interval_checked_at"] = second
        receipt["checked_at_advance_seconds"] = second - first
        receipt["elapsed_seconds"] = round(time.monotonic() - started, 3)
        if second - first < INTERVAL_SECONDS:
            raise RuntimeError("second execution occurred before the 60-second interval")
        exit_code = 0
    except Exception as exc:  # noqa: BLE001 - canary failure must survive in the receipt
        receipt["error"] = type(exc).__name__
        receipt["detail"] = str(exc)[:500]
    finally:
        if bootstrapped:
            bootout = invoke("/bin/launchctl", "bootout", service)
            receipt["bootout_rc"] = bootout.returncode
        absent = invoke("/bin/launchctl", "print", service)
        receipt["absent_print_rc"] = absent.returncode
        receipt["absence_proven"] = is_label_absent(absent)
        if not receipt["absence_proven"] or (bootstrapped and receipt["bootout_rc"] != 0):
            exit_code = 1
        if args.keep_fixture:
            receipt["fixture_root"] = str(root)
        else:
            shutil.rmtree(root, ignore_errors=True)

    return finish(receipt, args.receipt, exit_code)


def finish(receipt: dict[str, Any], path: Path | None, exit_code: int) -> int:
    rendered = json.dumps(receipt, sort_keys=True, indent=2) + "\n"
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
