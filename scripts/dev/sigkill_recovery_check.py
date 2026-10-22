#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 The qutebrowser developers
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""End-to-end SIGKILL crash-recovery check for the workspace journal (#7).

This script drives a *real* qutebrowser process.  It is NOT wired into CI: it
needs a display (or ``xvfb-run``), a working QtWebEngine, and it takes minutes.
The unit tests in ``tests/unit/workspaces/test_journal.py`` cover the same
logic in-process; this script is the only check that exercises the real
signal path (Qt, the SaveManager cycle, the statusbar prompt and the quit
handler).

Each trial does the following, in a fresh ``--basedir``:

1. Start qutebrowser with ``--tabs`` distinct ``data:`` URLs (opened as tabs,
   so they form a parent/child chain) and wait until the crash journal
   ``_journal.yml`` holds all of them.
2. Record the pre-crash topology from the journal: URL -> parent URL, and the
   active workspace name.
3. ``SIGKILL`` the main process (and its process group, so renderers go too).
4. Relaunch with the same basedir.  The recovery prompt must appear; answer it
   with ``:prompt-accept yes`` over IPC.
5. Wait for the journal to be rewritten by the recovery (its ``written_at``
   changes) and assert, from that journal:

   * every pre-crash URL is back as a tab entry (full restore);
   * every recovered entry's parent URL matches the pre-crash topology;
   * the active workspace name is unchanged.

A trial passes only when all three hold.  The run passes when at least
``--min-pass`` of ``--trials`` trials pass (default 4 of 5, as in #7).

Usage (requires a display; the trial takes a few seconds each)::

    .venv/bin/python scripts/dev/sigkill_recovery_check.py
    .venv/bin/python scripts/dev/sigkill_recovery_check.py --trials 1 --tabs 10
    xvfb-run -a .venv/bin/python scripts/dev/sigkill_recovery_check.py
    .venv/bin/python scripts/dev/sigkill_recovery_check.py --keep-temp

Exit status: 0 when the pass threshold is met, 1 when it is not, and 3 when
qutebrowser could not be driven at all (UNVERIFIED).
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

JOURNAL_NAME = "_journal.yml"
EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_UNVERIFIED = 3


@dataclass
class Trial:
    """Outcome of one SIGKILL-and-recover trial."""

    index: int
    started: bool = False  # True once qutebrowser wrote the pre-crash journal
    passed: bool = False
    reasons: list[str] = field(default_factory=list)


def _qutebrowser(basedir: Path, *args: str) -> list[str]:
    return [sys.executable, "-m", "qutebrowser", "--basedir", str(basedir), *args]


def _read_journal(path: Path) -> dict[str, Any] | None:
    """Return the journal payload, or None if missing or not yet readable.

    The checksum is not re-verified here: the journal is written atomically by
    qutebrowser, and a torn file simply fails to parse and is treated as
    "not yet written" while polling.
    """
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("payload"), dict):
        return None
    return data["payload"]


def _entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every tab entry across the journaled workspaces."""
    return [tab for ws in payload.get("workspaces", []) for tab in ws.get("tabs", [])]


def _topology(payload: dict[str, Any]) -> dict[str, str | None]:
    """Map each tab URL to its parent's URL (None for roots)."""
    entries = _entries(payload)
    url_by_id = {tab["tab_id"]: tab["url"] for tab in entries}
    return {
        tab["url"]: (url_by_id.get(tab["parent_id"]) if tab.get("parent_id") is not None else None)
        for tab in entries
    }


def _active_name(payload: dict[str, Any]) -> str | None:
    for ws in payload.get("workspaces", []):
        if ws.get("id") == payload.get("active_id"):
            return ws.get("name")
    return None


def _wait_for(predicate, timeout: float, interval: float = 0.25) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _ipc(basedir: Path, command: str) -> bool:
    """Send a ``:command`` to the running instance over IPC.

    Returns False when the call could not be delivered or hung.  A client that
    finds no listening instance starts a *new* qutebrowser instead, so callers
    must only use this once the relaunched instance is up (see ``--startup-wait``).
    """
    try:
        result = subprocess.run(
            _qutebrowser(basedir, command),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=20, check=False,
        )
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGKILL the main process and every child in its process group."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def _start_with_tabs(basedir: Path, expected: list[str], timeout: float,
                     journal: Path) -> subprocess.Popen | None:
    """Phase 1: start qutebrowser with *expected* tabs; wait for the journal.

    Returns the running process, or None (after killing it) when the journal
    never held all the tabs.
    """
    proc = subprocess.Popen(
        _qutebrowser(basedir, *expected),
        start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    def journal_full() -> bool:
        payload = _read_journal(journal)
        if payload is None:
            return False
        return set(expected) <= {tab["url"] for tab in _entries(payload)}

    if _wait_for(journal_full, timeout):
        return proc
    _kill_group(proc)
    return None


def _recover_and_check(
        basedir: Path, expected: list[str], journal: Path, timeout: float,
        before: dict[str, Any], trial: Trial, startup_wait: float,
) -> None:
    """Phases 4-5: relaunch, accept the prompt, and compare the journals."""
    crash_stamp = before.get("written_at")
    before_topology = _topology(before)
    before_active = _active_name(before)

    def recovered() -> bool:
        payload = _read_journal(journal)
        return (payload is not None
                and payload.get("written_at") != crash_stamp
                and set(expected) <= {tab["url"] for tab in _entries(payload)})

    def accept_and_check() -> bool:
        _ipc(basedir, ":prompt-accept yes")
        return recovered()

    # Give the relaunched instance time to open its IPC socket and raise the
    # prompt; an IPC call before that would start a second instance.
    time.sleep(startup_wait)
    if not _wait_for(accept_and_check, timeout, interval=1.5):
        trial.reasons.append("recovery prompt was not accepted / journal not restored")
        return

    after = _read_journal(journal)
    assert after is not None
    after_topology = _topology(after)
    missing = [url for url in expected if url not in after_topology]
    if missing:
        trial.reasons.append(f"{len(missing)} tab(s) not restored")
    mismatched = [
        url for url in expected
        if url in before_topology and before_topology[url] != after_topology.get(url)
    ]
    if mismatched:
        trial.reasons.append(f"parent topology changed for {len(mismatched)} tab(s)")
    if _active_name(after) != before_active:
        trial.reasons.append("active workspace changed")
    trial.passed = not trial.reasons


def run_trial(index: int, root: Path, tabs: int, timeout: float,
              startup_wait: float) -> Trial:
    """Run one SIGKILL-and-recover trial in ``root / trial-N``."""
    trial = Trial(index=index)
    basedir = root / f"trial-{index}"
    basedir.mkdir(parents=True)
    journal = basedir / "data" / "profiles" / "default" / "workspaces" / JOURNAL_NAME
    # Plain alphanumeric data: URLs, so the journal stores them unchanged.
    expected = [f"data:text/html,harness-tab-{n}" for n in range(tabs)]

    first = _start_with_tabs(basedir, expected, timeout, journal)
    if first is None:
        trial.reasons.append("journal never held all pre-crash tabs")
        return trial
    trial.started = True
    before = _read_journal(journal)
    assert before is not None

    # SIGKILL: no shutdown handler runs, so the journal is left behind.
    _kill_group(first)

    second = subprocess.Popen(
        _qutebrowser(basedir),
        start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        _recover_and_check(basedir, expected, journal, timeout, before, trial,
                           startup_wait)
        return trial
    finally:
        _ipc(basedir, ":quit")
        _kill_group(second)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="SIGKILL crash-recovery check for the workspace journal (#7).")
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--min-pass", type=int, default=4)
    parser.add_argument("--tabs", type=int, default=50,
                        help="tabs per workspace before the kill (default: 50)")
    parser.add_argument("--timeout", type=float, default=120.0,
                        help="seconds to wait for each phase (default: 120)")
    parser.add_argument("--startup-wait", type=float, default=10.0,
                        help="seconds to let the relaunched instance start (default: 10)")
    parser.add_argument("--keep-temp", action="store_true",
                        help="keep the temporary basedirs for inspection")
    args = parser.parse_args(argv)

    if shutil.which("xdpyinfo") is None and not os.environ.get("DISPLAY") \
            and not os.environ.get("QT_QPA_PLATFORM"):
        print("note: no DISPLAY set; qutebrowser will probably fail to start. "
              "Use xvfb-run or QT_QPA_PLATFORM=offscreen.", file=sys.stderr)

    root = Path(tempfile.mkdtemp(prefix="sigkill-recovery-"))
    print(f"basedirs under {root}")
    results: list[Trial] = []
    try:
        for index in range(1, args.trials + 1):
            try:
                trial = run_trial(index, root, args.tabs, args.timeout,
                                  args.startup_wait)
            except (OSError, subprocess.SubprocessError) as e:
                print(f"trial {index}: UNVERIFIED ({e})")
                return EXIT_UNVERIFIED
            results.append(trial)
            status = "PASS" if trial.passed else "FAIL"
            detail = "" if trial.passed else "; ".join(trial.reasons)
            print(f"trial {index}: {status} {detail}".rstrip())
    finally:
        if args.keep_temp:
            print(f"kept {root}")
        else:
            shutil.rmtree(root, ignore_errors=True)

    if not any(t.started for t in results):
        # qutebrowser never produced a journal: the harness could not drive it.
        print("UNVERIFIED: qutebrowser did not start in any trial")
        return EXIT_UNVERIFIED
    passed = sum(t.passed for t in results)
    print(f"{passed}/{len(results)} trials passed (need {args.min_pass})")
    return EXIT_PASS if passed >= args.min_pass else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
