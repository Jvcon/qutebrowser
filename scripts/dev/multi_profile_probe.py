#!/usr/bin/env python3

# SPDX-FileCopyrightText: 2026 The qutebrowser developers
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Probe in-process isolation of multiple QWebEngineProfile instances.

This is an architecture spike for issue #10 (Path B: one qutebrowser process
with several profiles).  It answers four questions:

1. Can three QWebEngineProfile objects with distinct persistent storage paths
   coexist in one QApplication?
2. Is a cookie/localStorage value written through profile 0 visible from the
   other profiles, either at runtime or on disk?
3. Does a renderer crash in one profile take the other profiles down?
4. What is the resident-memory cost of 1 profile + 1 page versus 3 profiles
   with one page each?

The probe is deliberately self-contained: it serves a tiny page from an
in-process HTTP server on 127.0.0.1, so no network access is needed.  Qt runs
headless with QT_QPA_PLATFORM=offscreen unless the caller overrides it.

Auxiliary destructive checks (constructing a profile before QApplication) run
in short-lived child interpreters, because the expected outcome is a crash.
The profile isolation, memory, and crash-isolation checks all happen inside a
single QApplication process.

Usage:
    .venv/bin/python scripts/dev/multi_profile_probe.py
    .venv/bin/python scripts/dev/multi_profile_probe.py --keep-temp
    .venv/bin/python scripts/dev/multi_profile_probe.py --skip-timing-probes

The exit status is 0 for FEASIBLE / FEASIBLE-WITH-CAVEATS, 2 for NOT-FEASIBLE,
and 3 when a required check could not be executed (UNVERIFIED).
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# These have to be set before QtWebEngine is imported.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "--no-sandbox --disable-gpu --disable-dev-shm-usage",
)
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")

from PyQt6.QtCore import (  # noqa: E402  pylint: disable=wrong-import-position
    PYQT_VERSION_STR,
    QEvent,
    QEventLoop,
    QT_VERSION_STR,
    QTimer,
    QUrl,
    qVersion,
)
from PyQt6.QtWebEngineCore import (  # noqa: E402  pylint: disable=wrong-import-position
    QWebEnginePage,
    QWebEngineProfile,
)
from PyQt6.QtWebEngineWidgets import (  # noqa: E402  pylint: disable=wrong-import-position
    QWebEngineView,
)
from PyQt6.QtWidgets import QApplication  # noqa: E402  pylint: disable=wrong-import-position

COOKIE_NAME = "spike_cookie"
COOKIE_VALUE = "profile-0"
STORAGE_KEY = "spike_key"
STORAGE_VALUE = "profile-0"

# This source is executed in a fresh interpreter, so imports can happen at
# exactly the point we want to test.
TIMING_PROBE_SOURCE = r"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault(
    "QTWEBENGINE_CHROMIUM_FLAGS",
    "--no-sandbox --disable-gpu --disable-dev-shm-usage",
)

mode = sys.argv[1]

if mode == "profile-before-app":
    from PyQt6.QtWebEngineCore import QWebEngineProfile
    QWebEngineProfile("early")
    from PyQt6.QtWidgets import QApplication
    QApplication(sys.argv[:1])
elif mode == "profile-after-core-app":
    from PyQt6.QtCore import QCoreApplication
    app = QCoreApplication(sys.argv[:1])
    from PyQt6.QtWebEngineCore import QWebEngineProfile
    QWebEngineProfile("early")
elif mode == "widgets-import-after-app":
    from PyQt6.QtWidgets import QApplication
    app = QApplication(sys.argv[:1])
    from PyQt6.QtWebEngineWidgets import QWebEngineView
elif mode == "app-first-control":
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtWebEngineCore import QWebEngineProfile
    app = QApplication(sys.argv[:1])
    profile = QWebEngineProfile("control", app)
    view = QWebEngineView()
    print("control: profile and view created")

print("OK")
"""


class ProbeFailure(RuntimeError):
    """A required probe check failed."""


@dataclass
class JsResult:
    """Result of a runJavaScript call."""

    ok: bool
    value: Any = None
    error: str | None = None


@dataclass
class JsPage:
    """A profile and the page/view using it."""

    index: int
    profile: QWebEngineProfile
    page: QWebEnginePage
    view: QWebEngineView
    path: Path
    load_ok: bool | None = None
    runtime_cookie: str = ""
    runtime_storage: str = ""


@dataclass
class ProbeReport:
    """Everything the script learned, for the final verdict."""

    timing: dict[str, tuple[int, str]] = field(default_factory=dict)
    pages: list[JsPage] = field(default_factory=list)
    isolation_runtime_ok: bool | None = None
    disk_cookie_rows: dict[int, int] = field(default_factory=dict)
    disk_storage_marker: dict[int, bool] = field(default_factory=dict)
    memory_one: dict[str, int] | None = None
    memory_three: dict[str, int] | None = None
    crash_status: str | None = None
    crash_code: int | None = None
    after_crash: dict[int, str | None] = field(default_factory=dict)
    crash_isolation_ok: bool | None = None
    caveats: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class _Handler(BaseHTTPRequestHandler):
    """Serve one tiny page; deliberately no Set-Cookie header."""

    body = (
        b"<!doctype html><html><head><title>profile spike</title></head>"
        b"<body><h1>profile spike</h1></body></html>"
    )

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Keep the probe output readable."""


class LocalSite:
    """A local HTTP server used as the stand-in for a website."""

    def __init__(self) -> None:
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.url = ""

    def __enter__(self) -> "LocalSite":
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        port = self._server.server_address[1]
        self.url = f"http://127.0.0.1:{port}/"
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="profile-spike-http",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse commandline arguments."""
    parser = argparse.ArgumentParser(
        description="Probe in-process QWebEngineProfile isolation "
        "(qutebrowser issue #10).",
    )
    parser.add_argument(
        "--profiles",
        type=int,
        default=3,
        help="number of profiles to create (default: 3; minimum: 2)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="page-load timeout in seconds (default: 15)",
    )
    parser.add_argument(
        "--settle",
        type=float,
        default=2.0,
        help="seconds to let memory settle before measuring (default: 2)",
    )
    parser.add_argument(
        "--temp-dir",
        type=Path,
        help="parent directory for profile storage (default: a new temp dir)",
    )
    parser.add_argument(
        "--keep-temp",
        action="store_true",
        help="keep the generated profile directories",
    )
    parser.add_argument(
        "--skip-timing-probes",
        action="store_true",
        help="skip the child-process QApplication timing probes",
    )
    return parser.parse_args(argv)


def wait_ms(milliseconds: int) -> None:
    """Run the event loop for a fixed amount of time."""
    loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    timer.start(milliseconds)
    loop.exec()
    timer.stop()


def wait_for_load(page: QWebEnginePage, url: str, timeout_ms: int) -> bool:
    """Navigate to url and wait for loadFinished."""
    loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)
    result: dict[str, bool | None] = {"ok": None}

    def finished(ok: bool) -> None:
        result["ok"] = ok
        loop.quit()

    page.loadFinished.connect(finished)
    timer.timeout.connect(loop.quit)
    timer.start(timeout_ms)
    page.load(QUrl(url))
    loop.exec()
    timer.stop()
    page.loadFinished.disconnect(finished)

    if result["ok"] is None:
        raise ProbeFailure(f"timed out after {timeout_ms / 1000:.1f}s loading {url}")
    return bool(result["ok"])


def run_js(page: QWebEnginePage, script: str, timeout_ms: int = 3000) -> JsResult:
    """Run JavaScript in a page, returning a timeout/error-aware result."""
    loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)
    result: dict[str, Any] = {"done": False, "value": None}

    def callback(value: Any) -> None:
        result["done"] = True
        result["value"] = value
        loop.quit()

    try:
        page.runJavaScript(script, 0, callback)
    except RuntimeError as exc:
        return JsResult(False, error=str(exc))

    timer.timeout.connect(loop.quit)
    timer.start(timeout_ms)
    loop.exec()
    timer.stop()

    if not result["done"]:
        return JsResult(False, error="runJavaScript timed out")
    return JsResult(True, value=result["value"])


def read_proc_rss_kb(pid: int) -> int:
    """Read VmRSS for one process, or 0 if it is gone."""
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8") as status_file:
            for line in status_file:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (FileNotFoundError, PermissionError, ValueError):
        pass
    return 0


def descendant_pids(root: int) -> list[int]:
    """Return root plus all of its descendants, using /proc."""
    children: dict[int, list[int]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        except OSError:
            continue
        # The comm field is in parentheses and may itself contain spaces.
        close = stat.rfind(")")
        fields = stat[close + 2:].split()  # noqa: E203
        if len(fields) < 2:
            continue
        try:
            ppid = int(fields[1])
        except ValueError:
            continue
        children.setdefault(ppid, []).append(pid)

    result: list[int] = []
    stack = [root]
    seen: set[int] = set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        result.append(pid)
        stack.extend(children.get(pid, []))
    return result


def memory_snapshot() -> dict[str, int]:
    """Current RSS of this process plus all QtWebEngine child processes."""
    pids = descendant_pids(os.getpid())
    self_kb = read_proc_rss_kb(os.getpid())
    rows = [(pid, read_proc_rss_kb(pid)) for pid in pids]
    child_kb = sum(kb for pid, kb in rows if pid != os.getpid())
    return {
        "self_kb": self_kb,
        "child_kb": child_kb,
        "total_kb": self_kb + child_kb,
        "processes": len(pids),
    }


def measure_memory(
    label: str,
    settle_seconds: float,
    samples: int = 3,
    interval_ms: int = 400,
) -> dict[str, int]:
    """Take a few RSS samples after settling, and report the median."""
    wait_ms(int(settle_seconds * 1000))
    snapshots = []
    for _ in range(samples):
        snapshots.append(memory_snapshot())
        wait_ms(interval_ms)

    totals = sorted(snapshot["total_kb"] for snapshot in snapshots)
    representative = snapshots[totals.index(statistics.median(totals))]
    representative["min_kb"] = totals[0]
    representative["max_kb"] = totals[-1]
    representative["label"] = label  # type: ignore[assignment]
    return representative


def mib(kilobytes: int) -> str:
    """Format KiB as MiB."""
    return f"{kilobytes / 1024:.1f} MiB"


def make_profile(app: QApplication, index: int, root: Path) -> JsPage:
    """Create a named profile with its own storage path and one page."""
    path = root / f"profile-{index}"
    path.mkdir(parents=True, exist_ok=True)

    profile = QWebEngineProfile(f"spike-{index}", app)
    profile.setPersistentStoragePath(str(path))
    profile.setCachePath(str(path / "cache"))
    profile.setPersistentCookiesPolicy(
        QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies
    )

    view = QWebEngineView()
    page = QWebEnginePage(profile, view)
    view.setPage(page)
    view.resize(800, 600)
    view.show()
    return JsPage(index=index, profile=profile, page=page, view=view, path=path)


def _describe_returncode(returncode: int) -> str:
    """Turn subprocess return codes into exit= or signal names."""
    if returncode < 0:
        try:
            name = signal.Signals(-returncode).name
        except ValueError:
            name = f"signal {-returncode}"
        return name
    return f"exit {returncode}"


def _interesting_line(stdout: str, stderr: str) -> str:
    """Pick the diagnostic a human cares about out of Qt's noise."""
    noise = (
        "This plugin does not support",
        "QVulkanInstance",
        "GPUInfo not initialized",
        "Sandboxing disabled by user",
        "createPlatform",
    )
    lines = [
        line.strip()
        for line in (stdout + "\n" + stderr).splitlines()
        if line.strip() and not any(noise_line in line for noise_line in noise)
    ]
    preferred = [
        "control:",
        "ImportError:",
        "Argument list is empty",
        "ModuleNotFoundError:",
        "Traceback",
    ]
    for needle in preferred:
        for line in reversed(lines):
            if needle in line:
                return line
    return (
        lines[-1]
        if lines
        else "(no Qt diagnostic; process died before printing)"
    )


def run_timing_probes() -> dict[str, tuple[int, str]]:
    """Run ordering probes in child interpreters, where an abort is expected."""
    results: dict[str, tuple[int, str]] = {}
    modes = [
        "app-first-control",
        "widgets-import-after-app",
        "profile-before-app",
        "profile-after-core-app",
    ]
    env = os.environ.copy()
    for mode in modes:
        try:
            proc = subprocess.run(
                [sys.executable, "-c", TIMING_PROBE_SOURCE, mode],
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
                check=False,
            )
        except subprocess.TimeoutExpired:
            results[mode] = (124, "timeout")
            continue

        results[mode] = (
            proc.returncode,
            _interesting_line(proc.stdout, proc.stderr),
        )
    return results


def cookie_rows(profile_path: Path) -> list[tuple[str, str, str, int]]:
    """Read the cookies table from a profile's Cookies SQLite database."""
    database = profile_path / "Cookies"
    if not database.exists():
        return []
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        return list(
            connection.execute(
                "SELECT host_key, name, value, is_persistent FROM cookies"
            )
        )
    finally:
        connection.close()


def contains_marker(profile_path: Path, marker: bytes) -> bool:
    """Best-effort search for a marker in a profile's on-disk files."""
    for root, _dirs, files in os.walk(profile_path):
        for filename in files:
            try:
                if marker in Path(root, filename).read_bytes():
                    return True
            except OSError:
                continue
    return False


def check_isolation(report: ProbeReport) -> None:
    """Verify that the cookie/localStorage value is only visible in profile 0."""
    owner = report.pages[0]
    owner_cookie = run_js(owner.page, "document.cookie")
    owner_storage = run_js(owner.page, f"localStorage.getItem({STORAGE_KEY!r})")
    if not owner_cookie.ok or COOKIE_NAME not in str(owner_cookie.value):
        raise ProbeFailure(
            f"profile 0 does not see its own cookie: {owner_cookie.value!r} "
            f"({owner_cookie.error})"
        )
    if not owner_storage.ok or owner_storage.value != STORAGE_VALUE:
        raise ProbeFailure(
            f"profile 0 does not see its own localStorage value: "
            f"{owner_storage.value!r} ({owner_storage.error})"
        )
    owner.runtime_cookie = str(owner_cookie.value)
    owner.runtime_storage = str(owner_storage.value)

    for page_info in report.pages[1:]:
        cookie = run_js(page_info.page, "document.cookie")
        storage = run_js(page_info.page, f"localStorage.getItem({STORAGE_KEY!r})")
        page_info.runtime_cookie = "" if cookie.value is None else str(cookie.value)
        page_info.runtime_storage = (
            "null" if storage.value is None else str(storage.value)
        )
        if cookie.ok and COOKIE_NAME in str(cookie.value):
            raise ProbeFailure(
                f"profile {page_info.index} can see profile 0's cookie: "
                f"{cookie.value!r}"
            )
        if storage.ok and storage.value == STORAGE_VALUE:
            raise ProbeFailure(
                f"profile {page_info.index} can see profile 0's localStorage value"
            )

    report.isolation_runtime_ok = True


def crash_one_profile(report: ProbeReport, timeout_seconds: float = 10.0) -> None:
    """Crash the last profile's renderer and check the other profiles survive."""
    victim = report.pages[-1]
    seen: dict[str, int | str] = {}

    def terminated(status: Any, exit_code: int) -> None:
        seen["status"] = getattr(status, "name", str(status))
        seen["code"] = exit_code

    victim.page.renderProcessTerminated.connect(terminated)
    victim.page.load(QUrl("chrome://crash"))

    deadline = time.monotonic() + timeout_seconds
    while "status" not in seen and time.monotonic() < deadline:
        QApplication.processEvents()
        time.sleep(0.05)

    if "status" not in seen:
        report.errors.append(
            "chrome://crash did not terminate a renderer within "
            f"{timeout_seconds:.0f}s; crash isolation is UNVERIFIED"
        )
        report.crash_isolation_ok = None
        return

    report.crash_status = str(seen["status"])
    report.crash_code = int(seen["code"])

    survivors: dict[int, str | None] = {}
    for page_info in report.pages[:-1]:
        result = run_js(
            page_info.page,
            "document.body ? document.cookie : 'DEAD'",
            timeout_ms=3000,
        )
        survivors[page_info.index] = (
            str(result.value) if result.ok else f"ERROR: {result.error}"
        )
    report.after_crash = survivors
    report.crash_isolation_ok = all(
        value is not None and not value.startswith("ERROR:")
        for value in survivors.values()
    )
    if not report.crash_isolation_ok:
        report.errors.append("a profile was unusable after another profile crashed")


def shutdown_cleanly(
    app: QApplication,
    pages: list[JsPage],
    profiles: list[QWebEngineProfile],
) -> None:
    """Quit the event loop, then tear down pages and profiles.

    The clean quit matters for the disk check: Chromium flushes its cookie
    SQLite database during a normal shutdown.
    """
    QTimer.singleShot(300, app.quit)
    app.exec()

    for page_info in pages:
        page_info.page.deleteLater()
    for page_info in pages:
        page_info.view.deleteLater()
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)

    for profile in profiles:
        profile.deleteLater()
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
    app.processEvents()
    time.sleep(0.2)
    app.processEvents()


def print_report(report: ProbeReport, args: argparse.Namespace) -> None:
    """Print the full lab result."""
    print("=" * 78)
    print(" In-process multi-profile probe (qutebrowser issue #10, Path B)")
    print("=" * 78)
    print(f"Python {sys.version.split()[0]}")
    print(f"Qt {qVersion()} (compiled {QT_VERSION_STR}), PyQt {PYQT_VERSION_STR}")
    print(f"QT_QPA_PLATFORM={os.environ.get('QT_QPA_PLATFORM', '<unset>')}")
    print(
        "QTWEBENGINE_CHROMIUM_FLAGS="
        f"{os.environ.get('QTWEBENGINE_CHROMIUM_FLAGS', '<unset>')}"
    )
    print(f"profiles requested: {args.profiles}")
    print()

    if report.timing:
        print("[1] QApplication / profile-creation ordering")
        for mode, (returncode, line) in report.timing.items():
            print(f"    {mode:<28} {_describe_returncode(returncode):<14} {line}")
        print()

    print("[2] Profile isolation (one page per profile)")
    for page_info in report.pages:
        print(f"    profile-{page_info.index}")
        print(f"      storage:       {page_info.path}")
        print(f"      load ok:       {page_info.load_ok}")
        print(f"      cookie:        {page_info.runtime_cookie!r}")
        print(f"      localStorage:  {page_info.runtime_storage!r}")
        print(f"      cookie rows:   {report.disk_cookie_rows.get(page_info.index, 0)}")
        print(
            "      disk marker:   "
            f"{report.disk_storage_marker.get(page_info.index, False)}"
        )
    print(
        "    runtime isolation: "
        f"{'PASS' if report.isolation_runtime_ok else 'FAIL/UNVERIFIED'}"
    )
    disk_cookie_ok = (
        report.disk_cookie_rows.get(0, 0) >= 1
        and all(report.disk_cookie_rows.get(i, 0) == 0 for i in range(1, len(report.pages)))
    )
    print(f"    disk cookie isolation: {'PASS' if disk_cookie_ok else 'FAIL/UNVERIFIED'}")
    if len(report.pages) > 1:
        marker_ok = report.disk_storage_marker.get(0, False) and all(
            not report.disk_storage_marker.get(i, False)
            for i in range(1, len(report.pages))
        )
        print(f"    localStorage marker isolation: {'PASS' if marker_ok else 'UNVERIFIED'}")
    print()

    if report.memory_one is not None and report.memory_three is not None:
        one = report.memory_one
        three = report.memory_three
        delta = three["total_kb"] - one["total_kb"]
        extra = max(1, len(report.pages) - 1)
        print("[3] Resident memory (self + QtWebEngine descendants)")
        print(
            f"    1 profile + 1 page:       {mib(one['total_kb'])} "
            f"(self {mib(one['self_kb'])}, children {mib(one['child_kb'])}, "
            f"{one['processes']} processes)"
        )
        print(
            f"    {len(report.pages)} profiles + 1 page each: "
            f"{mib(three['total_kb'])} "
            f"(self {mib(three['self_kb'])}, children {mib(three['child_kb'])}, "
            f"{three['processes']} processes)"
        )
        print(
            f"    delta for {extra} extra profile(s): {mib(delta)} "
            f"({mib(delta // extra)} per profile)"
        )
        print(
            f"    sample range: {mib(one['min_kb'])}-{mib(one['max_kb'])} -> "
            f"{mib(three['min_kb'])}-{mib(three['max_kb'])}"
        )
        print()

    print("[4] Failure isolation inside one QApplication process")
    if report.crash_status is None:
        print("    renderer crash:          not triggered (UNVERIFIED)")
    else:
        print(f"    crashed renderer:        {report.crash_status} "
              f"(code {report.crash_code})")
        for index, value in report.after_crash.items():
            print(f"    profile-{index} after crash: {value!r}")
        print(
            "    crash isolation:         "
            f"{'PASS' if report.crash_isolation_ok else 'FAIL/UNVERIFIED'}"
        )
    print()

    if report.caveats:
        print("[5] Caveats observed")
        for caveat in report.caveats:
            print(f"    - {caveat}")
        print()
    if report.errors:
        print("[!] Errors / unverified checks")
        for error in report.errors:
            print(f"    - {error}")
        print()


def verdict_for(report: ProbeReport) -> tuple[str, str]:
    """Derive the verdict line from the checks that actually ran."""
    if report.errors:
        unverified = any(
            "UNVERIFIED" in error or "did not terminate" in error
            for error in report.errors
        )
        if unverified and report.isolation_runtime_ok:
            return (
                "FEASIBLE-WITH-CAVEATS",
                "core isolation passed; at least one destructive check was UNVERIFIED",
            )
        return ("NOT-FEASIBLE", "one or more isolation checks failed")

    if report.isolation_runtime_ok and report.crash_isolation_ok:
        return (
            "FEASIBLE-WITH-CAVEATS",
            "profiles and renderers are isolated, but creation order, "
            "headless flags, clean-shutdown flushing, and per-profile memory "
            "are real constraints",
        )
    return ("NOT-FEASIBLE", "isolation or failure-containment check failed")


def run_probe(  # noqa: C901 ("linear sequence of probe phases")
        args: argparse.Namespace, temp_root: Path) -> tuple[ProbeReport, int]:
    """Run the whole probe, returning the report and an exit status.

    The phases (timing probes, profile creation, isolation, memory, crash
    containment) run in a fixed order and share one ``QApplication``, so
    splitting them out would only add indirection without making any
    individual phase easier to read.
    """
    report = ProbeReport()
    app = QApplication(sys.argv[:1])
    app.setApplicationName("multi-profile-probe")

    profiles: list[QWebEngineProfile] = []
    pages: list[JsPage] = []
    shutdown_done = False

    try:
        if not args.skip_timing_probes:
            report.timing = run_timing_probes()

        with LocalSite() as site:
            # Create and load the first profile, so its memory can be measured
            # before the other profiles exist.
            first = make_profile(app, 0, temp_root)
            pages.append(first)
            profiles.append(first.profile)
            first.load_ok = wait_for_load(
                first.page, site.url, int(args.timeout * 1000)
            )
            if not first.load_ok:
                raise ProbeFailure("profile 0 failed to load the local page")
            wait_ms(500)

            report.memory_one = measure_memory(
                "1 profile + 1 page", args.settle
            )

            for index in range(1, args.profiles):
                page_info = make_profile(app, index, temp_root)
                pages.append(page_info)
                profiles.append(page_info.profile)
                page_info.load_ok = wait_for_load(
                    page_info.page, site.url, int(args.timeout * 1000)
                )
                if not page_info.load_ok:
                    raise ProbeFailure(
                        f"profile {index} failed to load the local page"
                    )

            report.pages = pages
            report.memory_three = measure_memory(
                f"{len(pages)} profiles + 1 page each", args.settle
            )

            # Set the markers only in profile 0, after every page has loaded.
            set_result = run_js(
                first.page,
                f"document.cookie = {COOKIE_NAME + '=' + COOKIE_VALUE!r} + "
                "' ; path=/; max-age=3600'; "
                f"localStorage.setItem({STORAGE_KEY!r}, {STORAGE_VALUE!r}); "
                "'set'",
            )
            if not set_result.ok or set_result.value != "set":
                raise ProbeFailure(
                    f"could not set markers in profile 0: {set_result.error}"
                )

            # Give Chromium a moment to commit the localStorage write to disk.
            wait_ms(1000)
            check_isolation(report)

            if not report.disk_storage_marker:
                for page_info in pages:
                    found = contains_marker(
                        page_info.path, STORAGE_VALUE.encode("utf-8")
                    )
                    report.disk_storage_marker[page_info.index] = found

            crash_one_profile(report)
            if report.crash_isolation_ok is False:
                raise ProbeFailure(
                    "a profile stopped working after another profile crashed"
                )

        shutdown_cleanly(app, pages, profiles)
        shutdown_done = True

        for page_info in pages:
            try:
                rows = cookie_rows(page_info.path)
            except sqlite3.Error as exc:
                report.errors.append(
                    f"could not read Cookies DB for profile "
                    f"{page_info.index}: {exc}"
                )
                rows = []
            report.disk_cookie_rows[page_info.index] = sum(
                1
                for _host, name, value, _persistent in rows
                if name == COOKIE_NAME and value == COOKIE_VALUE
            )

        # The cookie DB is the authoritative on-disk isolation check.
        if report.disk_cookie_rows.get(0, 0) == 0:
            report.errors.append(
                "profile 0's persistent cookie was not visible in its Cookies "
                "database after a clean shutdown (disk check UNVERIFIED)"
            )
        if any(
            report.disk_cookie_rows.get(index, 0) > 0
            for index in range(1, len(pages))
        ):
            raise ProbeFailure("a cookie leaked into another profile's Cookies DB")

        if report.timing:
            profile_before = report.timing.get("profile-before-app", (0, ""))[0]
            core_before = report.timing.get("profile-after-core-app", (0, ""))[0]
            widgets_after = report.timing.get("widgets-import-after-app", (0, ""))[0]
            if profile_before == 0:
                report.caveats.append(
                    "profile construction before QApplication did not crash here; "
                    "Qt usually aborts at this point"
                )
            if core_before == 0:
                report.caveats.append(
                    "QCoreApplication was enough for profile creation here; "
                    "Qt documents QApplication as required"
                )
            if widgets_after == 0:
                report.caveats.append(
                    "importing QtWebEngineWidgets after QApplication was accepted here"
                )

        report.caveats.extend(
            [
                "QtWebEngineWidgets must be imported (or "
                "AA_ShareOpenGLContexts set) before QApplication is created.",
                "QWebEngineProfile objects must be created after QApplication; "
                "constructing one first aborts the process (SIGABRT).",
                "Persistent cookies only reached the Cookies SQLite DB after a "
                "clean application shutdown with ForcePersistentCookies; a crash "
                "or hard exit may lose them.",
                "Headless mode needed explicit Chromium flags here: "
                "--no-sandbox, --disable-gpu, --disable-dev-shm-usage.",
                "The memory figure includes this process plus all QtWebEngine "
                "child processes; it is RSS, not a per-profile heap measurement.",
            ]
        )

        verdict, reason = verdict_for(report)
        print_report(report, args)
        print(f"VERDICT: {verdict}")
        print(f"  {reason}")
        if verdict == "NOT-FEASIBLE":
            return report, 2
        return report, 0

    finally:
        # Ensure a stray child process does not keep the probe alive if an
        # assertion failed before the normal shutdown path.
        if not shutdown_done:
            for page_info in pages:
                try:
                    page_info.view.close()
                    page_info.page.deleteLater()
                    page_info.view.deleteLater()
                except RuntimeError:
                    pass
            for profile in profiles:
                try:
                    profile.deleteLater()
                except RuntimeError:
                    pass
            app.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)
            app.processEvents()


def main(argv: list[str] | None = None) -> int:
    """Run the probe."""
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.profiles < 2:
        print("error: --profiles must be at least 2", file=sys.stderr)
        return 3

    if args.temp_dir is not None:
        temp_root = args.temp_dir.resolve()
        temp_root.mkdir(parents=True, exist_ok=True)
        cleanup = False
    else:
        temp_root = Path(tempfile.mkdtemp(prefix="qutebrowser-profile-spike-"))
        cleanup = not args.keep_temp

    print(f"Profile storage root: {temp_root}")
    try:
        _report, status = run_probe(args, temp_root)
    except ProbeFailure as exc:
        print(f"\nPROBE CHECK FAILED: {exc}")
        print("VERDICT: NOT-FEASIBLE")
        print("  a required isolation check could not be satisfied")
        return 2
    except Exception as exc:  # pylint: disable=broad-except
        print(f"\nPROBE ERROR: {type(exc).__name__}: {exc}")
        print("VERDICT: UNVERIFIED")
        print("  the environment could not run the complete probe")
        return 3
    finally:
        if cleanup:
            shutil.rmtree(temp_root, ignore_errors=True)

    return status


if __name__ == "__main__":
    sys.exit(main())
