# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for qutebrowser.workspaces.model."""

import base64
import uuid

import pytest

from qutebrowser.workspaces import model


class TestGroup:

    def test_folded_default(self):
        group = model.Group(group_id=uuid.uuid4(), name='github')
        assert group.folded is False

    @pytest.mark.parametrize('folded', [False, True])
    def test_roundtrip(self, folded):
        group = model.Group(group_id=uuid.uuid4(), name='github',
                            folded=folded)
        assert model.Group.from_dict(group.to_dict()) == group

    def test_to_dict_is_primitive(self):
        group = model.Group(group_id=uuid.uuid4(), name='github')
        d = group.to_dict()
        assert isinstance(d['group_id'], str)
        assert set(d) == {'group_id', 'name', 'folded'}


class TestWorkspaceTab:

    @pytest.mark.parametrize('parent_id, group_id', [
        (None, None),
        (3, None),
        (None, uuid.uuid4()),
        (3, uuid.uuid4()),
    ])
    def test_roundtrip(self, parent_id, group_id):
        tab = model.WorkspaceTab(tab_id=1, parent_id=parent_id,
                                 group_id=group_id)
        assert model.WorkspaceTab.from_dict(tab.to_dict()) == tab

    def test_optionals_default_and_roundtrip_none(self):
        tab = model.WorkspaceTab(tab_id=7)
        d = tab.to_dict()
        assert d['parent_id'] is None
        assert d['group_id'] is None
        assert model.WorkspaceTab.from_dict(d) == tab

    def test_group_id_serialized_as_str(self):
        tab = model.WorkspaceTab(tab_id=1, group_id=uuid.uuid4())
        assert isinstance(tab.to_dict()['group_id'], str)


class TestWorkspace:

    def test_roundtrip_with_groups_and_tabs(self):
        group_a = model.Group(group_id=uuid.uuid4(), name='github',
                              folded=True)
        group_b = model.Group(group_id=uuid.uuid4(), name='docs')
        tabs = [
            model.WorkspaceTab(tab_id=1, group_id=group_a.group_id),
            model.WorkspaceTab(tab_id=2, parent_id=1),
            model.WorkspaceTab(tab_id=3, group_id=group_b.group_id),
        ]
        workspace = model.Workspace(
            id=uuid.uuid4(), name='product-rd',
            groups=[group_a, group_b], tabs=tabs,
            last_active='2026-01-01T00:00:00+00:00')

        restored = model.Workspace.from_dict(workspace.to_dict())
        assert restored == workspace

    def test_default_empty_roundtrip(self):
        workspace = model.Workspace(id=uuid.uuid4(), name='empty')
        restored = model.Workspace.from_dict(workspace.to_dict())
        assert restored == workspace
        assert restored.groups == []
        assert restored.tabs == []
        assert restored.last_active is None

    def test_missing_lists_are_tolerated(self):
        workspace = model.Workspace.from_dict(
            {'id': str(uuid.uuid4()), 'name': 'legacy'})
        assert workspace.groups == []
        assert workspace.tabs == []


class TestPinnedTab:

    @pytest.mark.parametrize('favicon', [None, b'', b'\x89PNG\r\n\x1a\n'])
    def test_favicon_roundtrip(self, favicon):
        tab = model.PinnedTab(
            tab_id=1, title='GitHub', url='https://github.com',
            favicon=favicon, last_active='2026-01-01T00:00:00+00:00')
        assert model.PinnedTab.from_dict(tab.to_dict()) == tab

    def test_favicon_is_base64_encoded(self):
        tab = model.PinnedTab(tab_id=1, title='t', url='u',
                              favicon=b'hello')
        assert tab.to_dict()['favicon'] == base64.b64encode(b'hello').decode(
            'ascii')

    def test_favicon_none_stays_none(self):
        tab = model.PinnedTab(tab_id=1, title='t', url='u')
        assert tab.to_dict()['favicon'] is None

    def test_last_active_defaults_to_empty_string(self):
        tab = model.PinnedTab(tab_id=1, title='t', url='u')
        assert tab.last_active == ''
        assert model.PinnedTab.from_dict(tab.to_dict()) == tab
