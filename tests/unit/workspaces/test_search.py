# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Tests for the pure workspace search logic (VP-P5)."""

import uuid

from qutebrowser.workspaces import search
from qutebrowser.workspaces.model import Group, Workspace, WorkspaceTab


class TestParseQuery:

    def test_empty(self):
        assert search.parse_query('') == []

    def test_whitespace(self):
        assert search.parse_query('   \t\n ') == []

    def test_mixed_cjk_and_english(self):
        assert search.parse_query('中文 Test') == ['中文', 'test']

    def test_case_fold(self):
        assert search.parse_query('HeLLo WORLD') == ['hello', 'world']


class TestScoreEntry:

    def _entry(self, **kwargs):
        return WorkspaceTab(tab_id=1, **kwargs)

    def _group(self, name='group'):
        return Group(group_id=uuid.uuid4(), name=name)

    def test_empty_terms_scores_zero(self):
        assert search.score_entry(self._entry(), self._group(), []) == 0

    def test_title_prefix(self):
        entry = self._entry(title='Python docs')
        assert search.score_entry(entry, self._group(), ['python']) == 100

    def test_title_contains(self):
        entry = self._entry(title='Learn Python today')
        assert search.score_entry(entry, self._group(), ['python']) == 50

    def test_description(self):
        entry = self._entry(title='Unrelated', description='All about Python')
        assert search.score_entry(entry, self._group(), ['python']) == 20

    def test_source(self):
        entry = self._entry(title='Unrelated', source='python.org')
        assert search.score_entry(entry, self._group(), ['python']) == 20

    def test_url(self):
        entry = self._entry(title='Unrelated', url='https://python.org/x')
        assert search.score_entry(entry, self._group(), ['python']) == 20

    def test_group_name(self):
        entry = self._entry(title='Unrelated')
        group = self._group(name='Python reading')
        assert search.score_entry(entry, group, ['python']) == 15

    def test_no_match(self):
        entry = self._entry(title='Unrelated', url='https://example.com')
        assert search.score_entry(entry, self._group(), ['python']) is None

    def test_multi_term_title_and_url(self):
        entry = self._entry(
            title='Python guide', url='https://example.com/docs')
        assert search.score_entry(
            entry, self._group(), ['python', 'docs']) == 120

    def test_all_terms_must_match(self):
        entry = self._entry(title='Python guide')
        assert search.score_entry(
            entry, self._group(), ['python', 'missing']) is None

    def test_standalone_entry(self):
        entry = self._entry(title='Python guide')
        assert search.score_entry(entry, None, ['python']) == 100


class TestSearchWorkspace:

    def _workspace(self):
        group_a = Group(group_id=uuid.uuid4(), name='Docs')
        group_b = Group(group_id=uuid.uuid4(), name='Web')
        entries = [
            WorkspaceTab(
                tab_id=1, group_id=group_a.group_id,
                title='Python docs', url='https://docs.example.org',
                created_at='2024-01-01T00:00:00+00:00'),
            WorkspaceTab(
                tab_id=2, group_id=group_a.group_id,
                title='Learning Python', url='https://example.com/python',
                created_at='2024-06-01T00:00:00+00:00'),
            WorkspaceTab(
                tab_id=3, group_id=group_a.group_id,
                title='Unrelated', url='https://example.com',
                created_at='2024-02-01T00:00:00+00:00'),
            WorkspaceTab(
                tab_id=4, group_id=group_b.group_id,
                title='Python weekly', url='https://news.example.com',
                description='Weekly Python news',
                created_at='2024-03-01T00:00:00+00:00'),
            WorkspaceTab(
                tab_id=5, group_id=group_b.group_id,
                title='CSS tricks', url='https://css.example.com',
                created_at='2024-04-01T00:00:00+00:00'),
            WorkspaceTab(
                tab_id=6, group_id=group_b.group_id,
                title='JS weekly', url='https://js.example.com',
                created_at='2024-05-01T00:00:00+00:00'),
        ]
        return Workspace(
            id=uuid.uuid4(),
            name='test',
            groups=[group_a, group_b],
            tabs=entries,
        )

    def test_only_matches_returned(self):
        hits = search.search_workspace(self._workspace(), ['python'])
        assert {hit.entry.tab_id for hit in hits} == {1, 2, 4}

    def test_sorted_by_score_desc(self):
        hits = search.search_workspace(self._workspace(), ['python'])
        assert [hit.entry.tab_id for hit in hits] == [4, 1, 2]
        assert hits[0].score > hits[1].score > hits[2].score

    def test_ties_broken_by_created_at_desc(self):
        workspace = Workspace(
            id=uuid.uuid4(),
            name='ties',
            tabs=[
                WorkspaceTab(
                    tab_id=1, title='Python older',
                    created_at='2024-01-01T00:00:00+00:00'),
                WorkspaceTab(
                    tab_id=2, title='Python newer',
                    created_at='2024-02-01T00:00:00+00:00'),
            ],
        )
        hits = search.search_workspace(workspace, ['python'])
        assert [hit.entry.tab_id for hit in hits] == [2, 1]

    def test_groups_without_hits_omitted(self):
        workspace = self._workspace()
        hits = search.search_workspace(workspace, ['css'])
        assert [hit.entry.tab_id for hit in hits] == [5]
        assert hits[0].group is not None
        assert hits[0].group.name == 'Web'

    def test_group_name_matches_all_members(self):
        group = Group(group_id=uuid.uuid4(), name='Alpha')
        workspace = Workspace(
            id=uuid.uuid4(),
            name='group-name',
            groups=[group],
            tabs=[WorkspaceTab(
                tab_id=1, group_id=group.group_id, title='Unrelated')],
        )
        hits = search.search_workspace(workspace, ['alpha'])
        assert [hit.entry.tab_id for hit in hits] == [1]
        assert hits[0].score == 15
        assert hits[0].group is group

    def test_standalone_entry_included(self):
        workspace = self._workspace()
        workspace.tabs.append(WorkspaceTab(
            tab_id=7, title='Standalone Python',
            created_at='2024-07-01T00:00:00+00:00'))
        hits = search.search_workspace(workspace, ['python'])
        standalone = [hit for hit in hits if hit.entry.tab_id == 7]
        assert len(standalone) == 1
        assert standalone[0].group is None

    def test_no_terms_returns_everything(self):
        workspace = self._workspace()
        hits = search.search_workspace(workspace, [])
        assert len(hits) == len(workspace.tabs)
        assert all(hit.score == 0 for hit in hits)

    def test_mixed_cjk_and_english(self):
        workspace = self._workspace()
        workspace.tabs.append(WorkspaceTab(
            tab_id=8, title='中文 Python 入门',
            created_at='2024-08-01T00:00:00+00:00'))
        hits = search.search_workspace(workspace, ['中文', 'python'])
        assert [hit.entry.tab_id for hit in hits] == [8]
