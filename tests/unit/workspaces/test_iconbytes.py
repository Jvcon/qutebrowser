# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for qutebrowser.workspaces.iconbytes."""

import base64

from qutebrowser.qt.core import Qt
from qutebrowser.qt.gui import QIcon, QPixmap

from qutebrowser.workspaces import iconbytes


PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'


def test_pixmap_roundtrips_through_base64(qapp):
    pixmap = QPixmap(16, 16)
    pixmap.fill(Qt.GlobalColor.red)
    icon = QIcon(pixmap)

    encoded = iconbytes.icon_to_png_base64(icon)

    assert encoded
    data = base64.b64decode(encoded)
    assert data.startswith(PNG_SIGNATURE)
    loaded = QPixmap()
    assert loaded.loadFromData(data)
    assert loaded.width() == 16
    assert loaded.height() == 16


def test_empty_icon_returns_empty_string(qapp):
    assert iconbytes.icon_to_png_base64(QIcon()) == ''
