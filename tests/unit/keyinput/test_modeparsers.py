# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>:
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for mode parsers."""

from qutebrowser.qt.core import Qt
from qutebrowser.qt.gui import QKeySequence

import pytest

from qutebrowser.keyinput import modeparsers, keyutils
from qutebrowser.config import configexc
from qutebrowser.utils import usertypes


@pytest.fixture
def commandrunner(stubs):
    return stubs.FakeCommandRunner()


class TestsNormalKeyParser:

    @pytest.fixture(autouse=True)
    def patch_stuff(self, monkeypatch, stubs, keyinput_bindings):
        """Set up mocks and read the test config."""
        monkeypatch.setattr(
            'qutebrowser.keyinput.basekeyparser.usertypes.Timer',
            stubs.FakeTimer)

    @pytest.fixture
    def keyparser(self, commandrunner):
        kp = modeparsers.NormalKeyParser(win_id=0, commandrunner=commandrunner)
        return kp

    def test_keychain(self, keyparser, commandrunner):
        """Test valid keychain."""
        # Press 'z' which is ignored because of no match
        # Then start the real chain
        chain = keyutils.KeySequence.parse('zba')
        for info in chain:
            keyparser.handle(info.to_event())
        assert commandrunner.commands == [('message-info ba', None)]
        assert not keyparser._sequence

    def test_partial_keychain_timeout(self, keyparser, config_stub,
                                      qtbot, commandrunner):
        """Test partial keychain timeout."""
        config_stub.val.input.partial_timeout = 100
        timer = keyparser._partial_timer
        assert not timer.isActive()

        # Press 'b' for a partial match.
        # Then we check if the timer has been set up correctly
        keyparser.handle(keyutils.KeyInfo(Qt.Key.Key_B, Qt.KeyboardModifier.NoModifier).to_event())
        assert timer.isSingleShot()
        assert timer.interval() == 100
        assert timer.isActive()

        assert not commandrunner.commands
        assert keyparser._sequence == keyutils.KeySequence.parse('b')

        # Now simulate a timeout and check the keystring has been cleared.
        with qtbot.wait_signal(keyparser.keystring_updated) as blocker:
            timer.timeout.emit()

        assert not commandrunner.commands
        assert not keyparser._sequence
        assert blocker.args == ['']


class TestHintKeyParser:

    @pytest.fixture
    def hintmanager(self, stubs):
        return stubs.FakeHintManager()

    @pytest.fixture
    def keyparser(self, config_stub, key_config_stub, commandrunner,
                  hintmanager):
        return modeparsers.HintKeyParser(win_id=0,
                                         hintmanager=hintmanager,
                                         commandrunner=commandrunner)

    @pytest.mark.parametrize('bindings, keychain, prefix, hint', [
        (
            ['aa', 'as'],
            'as',
            'a',
            'as'
        ),
        (
            ['21', '22'],
            '<Num+2><Num+2>',
            '2',
            '22'
        ),
        (
            ['äa', 'äs'],
            'äs',
            'ä',
            'äs'
        ),
        (
            ['не', 'на'],
            'не',
            '<Н>',
            'не',
        ),
    ])
    def test_match(self, keyparser, hintmanager,
                   bindings, keychain, prefix, hint, pyqt_enum_workaround):
        with pyqt_enum_workaround(keyutils.KeyParseError):
            keyparser.update_bindings(bindings)

        seq = keyutils.KeySequence.parse(keychain)
        assert len(seq) == 2

        # pylint: disable-next=no-member
        match = keyparser.handle(seq[0].to_event())
        assert match == QKeySequence.SequenceMatch.PartialMatch
        assert hintmanager.keystr == prefix

        # pylint: disable-next=no-member
        match = keyparser.handle(seq[1].to_event())
        assert match == QKeySequence.SequenceMatch.ExactMatch
        assert hintmanager.keystr == hint

    def test_match_key_mappings(self, config_stub, keyparser, hintmanager,
                                pyqt_enum_workaround):
        with pyqt_enum_workaround(configexc.ValidationError):
            config_stub.val.bindings.key_mappings = {'α': 'a', 'σ': 's'}
        keyparser.update_bindings(['aa', 'as'])

        seq = keyutils.KeySequence.parse('ασ')
        assert len(seq) == 2

        # pylint: disable-next=no-member
        match = keyparser.handle(seq[0].to_event())
        assert match == QKeySequence.SequenceMatch.PartialMatch
        assert hintmanager.keystr == 'a'

        # pylint: disable-next=no-member
        match = keyparser.handle(seq[1].to_event())
        assert match == QKeySequence.SequenceMatch.ExactMatch
        assert hintmanager.keystr == 'as'

    def test_command(self, keyparser, config_stub, hintmanager, commandrunner):
        config_stub.val.bindings.commands = {
            'hint': {'abc': 'message-info abc'}
        }

        keyparser.update_bindings(['xabcy'])

        steps = [
            (Qt.Key.Key_X, QKeySequence.SequenceMatch.PartialMatch, 'x'),
            (Qt.Key.Key_A, QKeySequence.SequenceMatch.PartialMatch, ''),
            (Qt.Key.Key_B, QKeySequence.SequenceMatch.PartialMatch, ''),
            (Qt.Key.Key_C, QKeySequence.SequenceMatch.ExactMatch, ''),
        ]
        for key, expected_match, keystr in steps:
            info = keyutils.KeyInfo(key, Qt.KeyboardModifier.NoModifier)
            match = keyparser.handle(info.to_event())
            assert match == expected_match
            assert hintmanager.keystr == keystr
            if key != Qt.Key.Key_C:
                assert not commandrunner.commands

        assert commandrunner.commands == [('message-info abc', None)]


class TestWorkspacePanelKeyParser:

    """Tests for the VerticalPanel workspace mode keyparser (Phase 2).

    The parser is a thin subclass of :class:`NormalKeyParser` that
    hard-codes the ``KeyMode`` to :attr:`usertypes.KeyMode.workspace_panel`.
    It reuses the partial-keychain and STARTCHARS machinery, so the
    tests here mainly pin the mode and the inheritance, then exercise
    a representative key sequence to confirm the command runner is
    wired the same way as the normal mode parser.
    """

    @pytest.fixture(autouse=True)
    def patch_stuff(self, monkeypatch, stubs, keyinput_bindings):
        """Set up mocks and read the test config."""
        monkeypatch.setattr(
            'qutebrowser.keyinput.basekeyparser.usertypes.Timer',
            stubs.FakeTimer)

    @pytest.fixture
    def keyparser(self, commandrunner):
        return modeparsers.WorkspacePanelKeyParser(
            win_id=0, commandrunner=commandrunner)

    def test_inherits_normal_keyparser(self, keyparser):
        """Test that WorkspacePanelKeyParser subclasses NormalKeyParser."""
        assert isinstance(keyparser, modeparsers.NormalKeyParser)

    def test_mode_is_workspace_panel(self, keyparser):
        """Test that the parser's mode is KeyMode.workspace_panel."""
        assert keyparser._mode == usertypes.KeyMode.workspace_panel

    def test_not_passthrough(self, keyparser):
        """Test that the parser is not a passthrough mode."""
        assert keyparser.passthrough is False

    def test_dispatches_command(self, keyparser, commandrunner):
        """Test that a keypress dispatches a panel-mode bound command."""
        chain = keyutils.KeySequence.parse('j')
        for info in chain:
            keyparser.handle(info.to_event())
        assert commandrunner.commands == [('message-info panel-down', None)]

    def test_filter_active_passes_escape_through(self, keyparser,
                                                 commandrunner):
        """While the filter row is open, Escape is not handled as a binding."""
        keyparser.set_filter_active(True)
        assert keyparser.passthrough is True
        info = keyutils.KeyInfo(
            Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
        match = keyparser.handle(info.to_event())
        assert match == QKeySequence.SequenceMatch.NoMatch
        assert not commandrunner.commands

    def test_filter_inactive_handles_escape(self, config_stub, keyparser,
                                            commandrunner):
        """After the filter closes, Escape is dispatched normally again."""
        config_stub.val.bindings.commands = {
            'workspace_panel': {'<Escape>': 'message-info leave'},
        }
        keyparser._read_config()
        info = keyutils.KeyInfo(
            Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)

        keyparser.set_filter_active(True)
        assert keyparser.handle(info.to_event()) == (
            QKeySequence.SequenceMatch.NoMatch)
        assert not commandrunner.commands

        keyparser.set_filter_active(False)
        assert keyparser.handle(info.to_event()) == (
            QKeySequence.SequenceMatch.ExactMatch)
        assert commandrunner.commands == [('message-info leave', None)]
