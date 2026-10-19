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

    def test_lifecycle_flags_default_to_false(self):
        group = model.Group(group_id=uuid.uuid4(), name='github')
        assert group.pinned is False
        assert group.archived is False

    def test_post_init_stamps_created_at(self):
        group = model.Group(group_id=uuid.uuid4(), name='github')
        assert group.created_at is not None
        # ISO 8601 UTC timestamps end in +00:00.
        assert group.created_at.endswith('+00:00')

    def test_post_init_keeps_explicit_created_at(self):
        group = model.Group(group_id=uuid.uuid4(), name='github',
                            created_at='2026-01-01T00:00:00+00:00')
        assert group.created_at == '2026-01-01T00:00:00+00:00'

    @pytest.mark.parametrize('folded', [False, True])
    def test_roundtrip(self, folded):
        group = model.Group(group_id=uuid.uuid4(), name='github',
                            folded=folded)
        assert model.Group.from_dict(group.to_dict()) == group

    def test_roundtrip_with_all_fields(self):
        group = model.Group(
            group_id=uuid.uuid4(), name='github', folded=True, pinned=True,
            archived=True, created_at='2026-01-01T00:00:00+00:00')
        restored = model.Group.from_dict(group.to_dict())
        assert restored == group
        assert restored.pinned is True
        assert restored.archived is True

    def test_from_dict_tolerates_old_format(self):
        group_id = uuid.uuid4()
        restored = model.Group.from_dict({
            'group_id': str(group_id),
            'name': 'legacy',
            'folded': True,
        })
        assert restored.group_id == group_id
        assert restored.name == 'legacy'
        assert restored.folded is True
        assert restored.pinned is False
        assert restored.archived is False
        assert restored.created_at is not None

    def test_to_dict_is_primitive(self):
        group = model.Group(group_id=uuid.uuid4(), name='github')
        d = group.to_dict()
        assert isinstance(d['group_id'], str)
        assert set(d) == {
            'group_id', 'name', 'folded', 'pinned', 'archived',
            'created_at',
        }


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

    def test_metadata_fields_default_empty(self):
        tab = model.WorkspaceTab(tab_id=1)
        assert tab.title == ''
        assert tab.url == ''
        assert tab.description == ''
        assert tab.source == ''
        assert tab.favicon is None
        assert tab.last_fetched is None
        assert tab.fetch_attempts == 0
        assert tab.last_error is None

    def test_open_status_defaults_to_suspended(self):
        assert model.WorkspaceTab(tab_id=1).open_status == 'SUSPENDED'

    def test_open_status_roundtrips(self):
        tab = model.WorkspaceTab(tab_id=1, open_status='OPEN')
        assert model.WorkspaceTab.from_dict(tab.to_dict()) == tab

    def test_from_dict_missing_open_status_defaults_suspended(self):
        tab = model.WorkspaceTab.from_dict({'tab_id': 1})
        assert tab.open_status == 'SUSPENDED'

    def test_from_dict_preserves_explicit_open_status(self):
        tab = model.WorkspaceTab.from_dict(
            {'tab_id': 1, 'open_status': 'OPEN'})
        assert tab.open_status == 'OPEN'

    def test_user_edited_always_has_three_keys(self):
        tab = model.WorkspaceTab(tab_id=1)
        assert tab.user_edited == {
            'title': False,
            'description': False,
            'source': False,
        }
        partial = model.WorkspaceTab(
            tab_id=1, user_edited={'title': True})
        assert partial.user_edited == {
            'title': True,
            'description': False,
            'source': False,
        }

    def test_roundtrip_with_all_metadata(self):
        tab = model.WorkspaceTab(
            tab_id=1,
            parent_id=2,
            group_id=uuid.uuid4(),
            title='GitHub',
            url='https://github.com/qutebrowser',
            description='A browser',
            source='github.com',
            favicon='iVBORw0KGgo=',
            user_edited={'title': True, 'description': False,
                         'source': True},
            last_fetched='2026-01-01T00:00:00+00:00',
            fetch_attempts=3,
            last_error='timeout',
        )
        restored = model.WorkspaceTab.from_dict(tab.to_dict())
        assert restored == tab
        assert restored.user_edited['title'] is True
        assert restored.user_edited['description'] is False

    def test_from_old_dict_uses_empty_defaults(self):
        tab = model.WorkspaceTab.from_dict(
            {'tab_id': 7, 'parent_id': None, 'group_id': None})
        assert tab.tab_id == 7
        assert tab.title == ''
        assert tab.url == ''
        assert tab.description == ''
        assert tab.source == ''
        assert tab.favicon is None
        assert tab.last_fetched is None
        assert tab.fetch_attempts == 0
        assert tab.last_error is None
        assert tab.user_edited == {
            'title': False,
            'description': False,
            'source': False,
        }

    def test_to_dict_is_primitive(self):
        tab = model.WorkspaceTab(tab_id=1, group_id=uuid.uuid4())
        d = tab.to_dict()
        assert {
            'tab_id', 'parent_id', 'group_id', 'title', 'url',
            'description', 'source', 'favicon', 'user_edited',
            'last_fetched', 'fetch_attempts', 'last_error', 'open_status',
        } <= set(d)
        assert all(not isinstance(value, uuid.UUID)
                   for value in d.values())


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

    def test_last_active_defaults_to_none(self):
        tab = model.PinnedTab(tab_id=1, title='t', url='u')
        assert tab.last_active is None
        assert model.PinnedTab.from_dict(tab.to_dict()) == tab

    def test_last_active_omitted_when_none(self):
        tab = model.PinnedTab(tab_id=1, title='t', url='u')
        d = tab.to_dict()
        assert 'last_active' not in d
        assert model.PinnedTab.from_dict(d).last_active is None

    def test_last_active_roundtrips_when_set(self):
        tab = model.PinnedTab(tab_id=1, title='t', url='u',
                              last_active='2026-01-01T00:00:00+00:00')
        d = tab.to_dict()
        assert d['last_active'] == '2026-01-01T00:00:00+00:00'
        assert model.PinnedTab.from_dict(d) == tab
