# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

import base64
import dataclasses
import gc
import weakref

import pytest
pytest.importorskip('qutebrowser.qt.webenginecore')
from qutebrowser.qt import machinery, sip
from qutebrowser.qt.core import QObject, pyqtSignal
from qutebrowser.qt.webenginecore import QWebEngineDownloadRequest, QWebEngineProfile

from qutebrowser.utils import urlutils, usertypes, utils
from qutebrowser.browser import downloads
from qutebrowser.browser.webengine import webenginedownloads


@pytest.mark.parametrize('path, expected', [
    ('foo(1)', 'foo'),
    ('foo (1)', 'foo'),
    ('foo - 1970-01-01T00:00:00.000Z', 'foo'),
    ('foo(a)', 'foo(a)'),
    ('foo1', 'foo1'),
    ('foo%20bar', 'foo%20bar'),
    ('foo%2Fbar', 'foo%2Fbar'),
])
def test_strip_suffix(path, expected):
    assert webenginedownloads._strip_suffix(path) == expected


@dataclasses.dataclass
class _ExpectedNames:

    """The filenames used in the tests."""

    before: str
    after: str


class _FakeDownloadRequest(QObject):

    """Stands in for QWebEngineDownloadRequest, which can't be constructed.

    Only the progress signals and byte getters DownloadItem uses are modelled.
    """

    receivedBytesChanged = pyqtSignal()  # noqa: N815 - mimics Qt name
    totalBytesChanged = pyqtSignal()  # noqa: N815 - mimics Qt name
    stateChanged = pyqtSignal(QWebEngineDownloadRequest.DownloadState)  # noqa: N815 - mimics Qt name

    def __init__(self, parent=None):
        super().__init__(parent)
        self.getter_calls = 0

    def receivedBytes(self):
        self.getter_calls += 1
        return 5

    def totalBytes(self):
        self.getter_calls += 1
        return 10


@pytest.mark.skipif(machinery.IS_QT5, reason="Qt 6 progress signals only")
class TestProgressSlots:

    """Regression for #51: progress bindings must not capture the owner.

    The lambdas they replaced closed a reference cycle through the Qt-held
    connection.  The cycle GC could then clear the callable while Qt still
    delivered to it.  This only guards arity and delivery: it cannot detect
    the collector clearing the callable, which is timing-dependent.
    """

    @pytest.fixture
    def manager(self, qapp):
        manager = downloads.AbstractDownloadManager(parent=qapp)
        yield manager
        manager.deleteLater()

    @pytest.fixture
    def qt_item(self, qapp):
        item = _FakeDownloadRequest(parent=qapp)
        yield item
        # DownloadItem may already have deleted it via destroyed -> deleteLater.
        if not sip.isdeleted(item):
            item.deleteLater()

    @pytest.mark.parametrize('signal_name', ['receivedBytesChanged',
                                             'totalBytesChanged'])
    def test_delivers_progress_to_live_owner(self, qtbot, manager, qt_item,
                                             signal_name):
        item = webenginedownloads.DownloadItem(qt_item, manager)
        with qtbot.captureExceptions() as excs:
            getattr(qt_item, signal_name).emit()
        assert excs == []
        assert item.stats.done == 5
        assert item.stats.total == 10

    @pytest.mark.parametrize('signal_name', ['receivedBytesChanged',
                                             'totalBytesChanged'])
    def test_dead_owner_is_a_noop(self, qtbot, manager, qt_item, signal_name):
        """Once the owner is gone the still-connected slot delivers nothing."""
        item = webenginedownloads.DownloadItem(qt_item, manager)
        owner_ref = weakref.ref(item)
        # The wrapper is sip-owned by its manager parent, so only deleting the
        # C++ object lets the Python owner go away.
        item.deleteLater()
        del item
        qtbot.waitUntil(lambda: owner_ref() is None)
        gc.collect()

        qt_item.getter_calls = 0
        with qtbot.captureExceptions() as excs:
            getattr(qt_item, signal_name).emit()
        assert excs == []
        assert qt_item.getter_calls == 0


class TestDataUrlWorkaround:

    """With data URLs, we get rather weird base64 filenames back from QtWebEngine.

    See https://bugreports.qt.io/browse/QTBUG-90355
    """

    @pytest.fixture(params=[True, False])
    def pdf_bytes(self, request):
        with_slash = request.param

        # https://stackoverflow.com/a/17280876/2085149
        pdf_source = [
            '%PDF-1.0',
            '1 0 obj<</Pages 2 0 R>>endobj',
            '2 0 obj<</Kids[3 0 R]/Count 1>>endobj',
            '3 0 obj<</MediaBox[0 0 3 3]>>endobj',
            'trailer<</Root 1 0 R>>',
        ]

        if with_slash:
            pdf_source.insert(1, '% ?')  # this results in a slash in base64

        return '\n'.join(pdf_source).encode('ascii')

    @pytest.fixture
    def pdf_url(self, pdf_bytes):
        return urlutils.data_url('application/pdf', pdf_bytes)

    @pytest.fixture
    def expected_names(self, webengine_versions, pdf_bytes):
        """Get the expected filenames before/after the workaround.

        With QtWebEngine 5.15.3, this is handled correctly inside QtWebEngine
        and we get a qwe_download.pdf instead.
        """
        if webengine_versions.webengine >= utils.VersionNumber(5, 15, 3):
            return _ExpectedNames(before='qwe_download.pdf', after='qwe_download.pdf')

        with_slash = b'% ?' in pdf_bytes
        base64_data = base64.b64encode(pdf_bytes).decode('ascii')

        if with_slash:
            assert '/' in base64_data
            before = base64_data.split('/')[1]
        else:
            assert '/' not in base64_data
            before = 'pdf'  # from the mimetype

        return _ExpectedNames(before=before, after='download.pdf')

    @pytest.fixture
    def webengine_profile(self, qapp):
        profile = QWebEngineProfile.defaultProfile()
        profile.setParent(qapp)
        return profile

    @pytest.fixture
    def download_manager(self, qapp, qtbot, webengine_profile, download_tmpdir,
                         config_stub):
        config_stub.val.downloads.location.suggestion = 'filename'
        manager = webenginedownloads.DownloadManager(parent=qapp)
        manager.install(webengine_profile)
        yield manager
        webengine_profile.downloadRequested.disconnect()
        manager.deleteLater()

    def test_workaround(self, webengine_tab, message_mock, qtbot,
                        pdf_url, download_manager, expected_names):
        """Verify our workaround works properly."""
        with qtbot.wait_signal(message_mock.got_question):
            webengine_tab.load_url(pdf_url)

        question = message_mock.get_question()
        assert question.default == expected_names.after

    def test_explicit_filename(self, webengine_tab, message_mock, qtbot,
                               pdf_url, download_manager):
        """If a website sets an explicit filename, we should respect that."""
        pdf_url_str = pdf_url.toDisplayString()
        html = f'<a href="{pdf_url_str}" download="filename.pdf" id="link">'

        with qtbot.wait_signal(webengine_tab.load_finished):
            webengine_tab.set_html(html)

        with qtbot.wait_signal(message_mock.got_question):
            webengine_tab.elements.find_id(
                "link",
                lambda elem: elem.click(usertypes.ClickTarget.normal),
            )

        question = message_mock.get_question()
        assert question.default == 'filename.pdf'

    def test_workaround_needed(self, qtbot, webengineview,
                               pdf_url, expected_names, webengine_profile):
        """Verify that our workaround for this is still needed.

        In other words, check whether we get those base64-filenames rather than a
        "download.pdf" like with Chromium.
        """
        def check_item(item):
            assert item.mimeType() == 'application/pdf'
            assert item.url().scheme() == 'data'
            assert item.downloadFileName() == expected_names.before

            return True

        with qtbot.wait_signal(webengine_profile.downloadRequested,
                              check_params_cb=check_item):
            webengineview.load(pdf_url)
