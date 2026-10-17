# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Encode a :class:`QIcon` as base64 PNG bytes for workspace persistence.

Workspace favicons are stored as base64 ASCII inside the YAML files (see
:attr:`qutebrowser.workspaces.model.WorkspaceTab.favicon`), mirroring the
existing :class:`~qutebrowser.workspaces.model.PinnedTab` round-trip.
"""

import base64

from qutebrowser.qt.core import QBuffer, QIODevice
from qutebrowser.qt.gui import QIcon


def icon_to_png_base64(icon: QIcon, size: int = 16) -> str:
    """Render *icon* at *size* and return base64-encoded PNG bytes.

    Returns an empty string when the icon is unset/empty or cannot be
    rendered, so callers can treat it as "no favicon".
    """
    if icon is None or icon.isNull():
        return ''

    pixmap = icon.pixmap(size, size)
    if pixmap.isNull():
        return ''

    buffer = QBuffer()
    if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
        return ''
    try:
        if not pixmap.save(buffer, 'PNG'):
            return ''
        data = bytes(buffer.data())
    finally:
        buffer.close()

    if not data:
        return ''
    return base64.b64encode(data).decode('ascii')
