# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Best-effort metadata helpers for workspace tab entries (VP-F1).

Description capture cannot be synchronous: ``AbstractTab.run_js`` is
callback-based.  :func:`fetch_description` therefore supports both an
asynchronous callback (used by the bridge) and a blocking convenience call
(used by tests / one-shot tooling).  Any failure or timeout resolves to an
empty string, never an exception.
"""

from collections.abc import Callable
from typing import Optional

from qutebrowser.qt.core import QEventLoop, QTimer

#: Pull the standard meta description, if any.
DESCRIPTION_JS = (
    "document.querySelector('meta[name=\"description\"]')?.content || ''"
)


def fetch_description(  # noqa: C901
    tab: object,
    callback: Optional[Callable[[str], None]] = None,
    *,
    timeout_ms: int = 2000,
    on_error: Optional[Callable[[str], None]] = None,
) -> Optional[str]:
    """Fetch ``meta[name=description]`` from *tab*.

    Args:
        tab: An ``AbstractTab``-like object exposing ``run_js(code, cb)``.
        callback: Called exactly once with the description (or ``''``).
            When omitted, this function blocks on a local event loop and
            returns the description instead.
        timeout_ms: Milliseconds to wait before resolving to ``''``.
        on_error: Called with a short error string if ``run_js`` raises.

    Returns:
        The description when *callback* is omitted; otherwise ``None``.
    """
    loop: Optional[QEventLoop] = None
    result: list[str] = []

    def finish(value: object) -> None:
        nonlocal finished
        if finished:
            return
        finished = True
        timer.stop()
        text = value if isinstance(value, str) else ''
        if callback is not None:
            callback(text)
        else:
            result.append(text)
            if loop is not None:
                loop.quit()

    finished = False
    try:
        timer = QTimer(tab)  # type: ignore[arg-type]
    except TypeError:
        # Non-QObject tab doubles (unit tests) can't own the timer.
        timer = QTimer()
    timer.setSingleShot(True)

    def on_timeout() -> None:
        finish('')

    timer.timeout.connect(on_timeout)
    timer.start(timeout_ms)

    if callback is None:
        loop = QEventLoop()

    try:
        tab.run_js(DESCRIPTION_JS, finish)  # type: ignore[attr-defined]
    except Exception as e:  # noqa: BLE001 - best-effort metadata capture
        if on_error is not None:
            on_error(str(e)[:200])
        finish('')

    if callback is not None:
        return None

    if not finished and loop is not None:
        loop.exec()
    return result[0] if result else ''
