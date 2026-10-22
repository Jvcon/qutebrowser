# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Panel-side performance baselines for the vertical workspace (issue #38).

These benchmarks record the regression reference numbers quoted in ADR 0008
§ "Performance baselines".  They measure the *panel* only (WorkspaceManager +
TabTree under ``QT_QPA_PLATFORM=offscreen``).  They do not measure browser
process memory or SIGKILL recovery (issue #7).

Measured operations:

* TabTree rebuild at 1 / 10 / 100 / 500 tabs (``manager.changed`` path).
* Group collapse and expand (``TabTreeModel.toggle_folded``).
* Workspace switch (``WorkspaceManager.set_active`` -> tree rebuild).
* Filter row open, and filter typing (one ``textChanged`` per character,
  routed like :class:`VerticalPanel` does into ``TabTree.set_filter``).
* ``WorkspaceManager.auto_suspend_due_tabs`` over 500 tabs.

Persistence is an in-memory stand-in so no benchmark measures disk I/O.
"""

import gc
import tracemalloc
import uuid

import pytest

from qutebrowser.mainwindow import filterrow, tabtree
from qutebrowser.workspaces.manager import WorkspaceManager
from qutebrowser.workspaces.model import WorkspaceTab
from qutebrowser.workspaces.persistence import (PinnedPersistence,
                                                WorkspacePersistence)

TAB_COUNTS = [1, 10, 100, 500]
TABS_PER_GROUP = 25
FILTER_QUERY = 'item/4'  # typed one character at a time


class MemoryPersistence:

    """In-memory stand-in for the workspace / pinned persistence objects.

    Shared with the memory-budget tests in spirit: the benchmarked code path
    contains no file I/O, so timings reflect panel work only.
    """

    profile_name = 'default'

    def __init__(self) -> None:
        self.state: tuple = (None, {})

    def load(self, workspace_id):
        raise FileNotFoundError(str(workspace_id))

    def load_all(self):
        return []

    def save(self, workspace):
        pass

    def delete(self, workspace_id):
        pass

    def load_state(self):
        return self.state[0]

    def load_state_data(self):
        return self.state

    def save_state(self, active_id, last_active_tabs=None):
        self.state = (active_id, dict(last_active_tabs or {}))


def populate_workspace(manager, n_tabs, name='bench'):
    """Create a workspace holding *n_tabs* entries and return its id.

    Roughly one third of the tabs are grouped (``TABS_PER_GROUP`` tabs per
    group), every seventh tab is a child of its predecessor, and every tab is
    OPEN so the tree shows the whole workspace.
    """
    workspace = manager.add_workspace_with_id(uuid.uuid4(), name)
    n_groups = n_tabs // TABS_PER_GROUP
    groups = [manager.create_group(workspace.id, f'group {g}')
              for g in range(n_groups)]
    for i in range(n_tabs):
        group_id = None
        if groups and i % 3 == 0:
            group_id = groups[i % n_groups].group_id
        parent_id = i - 1 if i % 7 == 6 else None
        manager.add_tab(workspace.id, WorkspaceTab(
            tab_id=i,
            parent_id=parent_id,
            group_id=group_id,
            title=f'Item {i}',
            url=f'https://site{i % 50}.example.com/item/{i}',
            open_status='OPEN',
            reading_status='FRESH',
        ))
    return workspace.id


@pytest.fixture
def manager(qapp):
    return WorkspaceManager(MemoryPersistence(), MemoryPersistence())


@pytest.fixture
def tree_factory(qtbot, config_stub, manager):
    """Build a real TabTree bound to *manager* (not shown; offscreen)."""
    def _make():
        tree = tabtree.TabTree(0, manager)
        qtbot.add_widget(tree)
        return tree
    return _make


class TestTabTreeRebuild:

    @pytest.mark.parametrize('n_tabs', TAB_COUNTS)
    def test_rebuild(self, benchmark, manager, tree_factory, n_tabs):
        """Full TabTree rebuild of the active workspace via ``changed``."""
        ws_id = populate_workspace(manager, n_tabs)
        manager.set_active(ws_id)
        tree = tree_factory()

        # ``changed`` is what every mutation emits; the tree reacts by
        # rebuilding its model from scratch.
        benchmark(manager.changed.emit)

        assert tree.model().rowCount() > 0


class TestGroupFold:

    @pytest.fixture
    def tree(self, manager, tree_factory):
        ws_id = populate_workspace(manager, 500)
        manager.set_active(ws_id)
        return tree_factory()

    @staticmethod
    def _first_group_index(model):
        index = model.index(0, 0)
        assert model.group_for_index(index) is not None
        return index

    def test_collapse(self, benchmark, tree):
        """Collapse one group in a 500-tab workspace."""
        model = tree.model()
        index = self._first_group_index(model)
        group = model.group_for_index(index)

        def setup():
            group.folded = False

        benchmark.pedantic(model.toggle_folded, args=(index,),
                           setup=setup, rounds=50, iterations=1)
        assert group.folded is True

    def test_expand(self, benchmark, tree):
        """Expand one group in a 500-tab workspace."""
        model = tree.model()
        index = self._first_group_index(model)
        group = model.group_for_index(index)

        def setup():
            group.folded = True

        benchmark.pedantic(model.toggle_folded, args=(index,),
                           setup=setup, rounds=50, iterations=1)
        assert group.folded is False


class TestWorkspaceSwitch:

    def test_switch_500_tabs(self, benchmark, manager, tree_factory):
        """Switch between two 500-tab workspaces (set_active + rebuild)."""
        first = populate_workspace(manager, 500, name='first')
        second = populate_workspace(manager, 500, name='second')
        manager.set_active(first)
        tree = tree_factory()
        targets = [second, first]
        counter = iter(range(10**9))

        def switch():
            manager.set_active(targets[next(counter) % 2])

        benchmark.pedantic(switch, rounds=40, iterations=1)
        assert tree.model().rowCount() > 0


class TestFilter:

    @pytest.fixture
    def panel(self, manager, tree_factory, qtbot):
        """A FilterRow wired to a 500-tab TabTree like VerticalPanel does."""
        ws_id = populate_workspace(manager, 500)
        manager.set_active(ws_id)
        tree = tree_factory()
        row = filterrow.FilterRow()
        qtbot.add_widget(row)
        row.filter_changed.connect(tree.set_filter)
        return row, tree

    def test_open(self, benchmark, panel):
        """Open the filter row (clear, show, focus)."""
        row, _tree = panel

        def setup():
            row.close_filter()

        benchmark.pedantic(row.open, setup=setup, rounds=50, iterations=1)
        assert not row.isHidden()

    def test_typing_500_tabs(self, benchmark, panel):
        """Type FILTER_QUERY one character at a time into a 500-tab tree.

        Each character is a text change, so each one rebuilds the filtered
        model.  The reported median is for the whole query; divide by
        ``len(FILTER_QUERY)`` for the per-keystroke cost.
        """
        row, tree = panel

        def type_query():
            row.open()
            for end in range(1, len(FILTER_QUERY) + 1):
                row.setText(FILTER_QUERY[:end])

        def setup():
            row.close_filter()
            tree.set_filter('')

        benchmark.pedantic(type_query, setup=setup, rounds=20, iterations=1)
        assert tree.model().rowCount() > 0


class TestAutoSuspendScan:

    @pytest.fixture
    def ws_id(self, manager):
        ws_id = populate_workspace(manager, 500)
        for tab in manager.get_workspace(ws_id).tabs:
            tab.reading_status = 'DONE'
            tab.done_at = '2000-01-01T00:00:00+00:00'
        return ws_id

    def test_scan_500_all_due(self, benchmark, manager, ws_id):
        """Full scan over 500 tabs where every entry is due for suspension."""
        tabs = manager.get_workspace(ws_id).tabs

        def setup():
            for tab in tabs:
                tab.open_status = 'OPEN'

        result = benchmark.pedantic(
            manager.auto_suspend_due_tabs, args=(ws_id,), kwargs={'days': 7},
            setup=setup, rounds=20, iterations=1)
        assert result == 500

    def test_scan_500_none_due(self, benchmark, manager, ws_id):
        """Pure scan cost: no entry is due (reading state not DONE)."""
        for tab in manager.get_workspace(ws_id).tabs:
            tab.reading_status = 'FRESH'
        result = benchmark(manager.auto_suspend_due_tabs, ws_id, days=7)
        assert result == 0


# --- memory budget (issue #38) ------------------------------------------------
#
# Measured with ``tracemalloc``: it is deterministic, needs no psutil, and
# reports exactly the Python-heap bytes retained by the panel's objects
# (WorkspaceManager, Workspace, Group, WorkspaceTab).  It does not see Qt's
# C++ allocations (widgets, pixmaps) nor browser-process memory; those are
# out of scope for the panel budget (browser-level recovery is #7).
#
# Budgets are the measured value plus headroom; see ADR 0008 § "Performance
# baselines" for the recorded numbers.

#: Retained Python heap for one manager holding a 500-entry active workspace.
BUDGET_500_TABS_BYTES = 512 * 1024  # measured ~323 KiB

#: Retained Python heap after ``load()`` on a profile with 10 workspaces of
#: 500 entries each: only the active one may be resident.
BUDGET_10_WORKSPACES_LAZY_BYTES = 640 * 1024  # measured ~444 KiB


def _traced_retained(build):
    """Run *build* under tracemalloc; return ``(retained_bytes, result)``.

    The result is returned so the caller keeps it alive until the retained
    size has been read.
    """
    gc.collect()
    tracemalloc.start()
    try:
        result = build()
        gc.collect()
        current, _peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return current, result


def _report(label, measured, budget):
    line = (f"{label}: measured {measured / 1024:.1f} KiB, "
            f"budget {budget / 1024:.1f} KiB "
            f"(headroom {(budget - measured) / 1024:+.1f} KiB)")
    print(line)
    return line


class TestMemoryBudget:

    def test_500_tab_entries_within_budget(self, qapp):
        """500 tab entries in the active workspace fit the budget."""
        def build():
            manager = WorkspaceManager(MemoryPersistence(), MemoryPersistence())
            populate_workspace(manager, 500)
            return manager

        measured, manager = _traced_retained(build)
        assert len(manager.get_active().tabs) == 500
        line = _report('500 tab entries', measured, BUDGET_500_TABS_BYTES)
        assert measured <= BUDGET_500_TABS_BYTES, line

    def test_lazy_load_only_active_workspace(self, qapp, tmp_path,
                                             monkeypatch):
        """10 workspaces on disk: only the active one is resident after load.

        Verified two ways: the public ``list_workspaces()`` view holds one
        workspace, and only the active workspace's 500 ``WorkspaceTab``
        entries are materialised (counted via ``WorkspaceTab.from_dict``).
        """
        profile = tmp_path / 'profile'
        persistence = WorkspacePersistence(base_path=profile / 'workspaces')
        pinned = PinnedPersistence(path=profile / 'pinned.yml')
        seed = WorkspaceManager(persistence, pinned)
        ids = [populate_workspace(seed, 500, name=f'ws-{n}')
               for n in range(10)]
        seed.save()
        active_id, others = ids[0], ids[1:]

        # A counter rather than a list: retaining the raw dicts would be
        # charged to the manager by tracemalloc.
        materialised = [0]
        original = WorkspaceTab.from_dict

        def counting(cls, data):
            materialised[0] += 1
            return original.__func__(cls, data)

        monkeypatch.setattr(WorkspaceTab, 'from_dict',
                            classmethod(counting))

        manager = WorkspaceManager(persistence, pinned)

        measured, _ = _traced_retained(lambda: manager.load() or manager)
        assert [ws.id for ws in manager.list_workspaces()] == [active_id]
        assert materialised[0] == 500
        line = _report('load() with 10 on-disk workspaces (lazy)',
                       measured, BUDGET_10_WORKSPACES_LAZY_BYTES)
        assert measured <= BUDGET_10_WORKSPACES_LAZY_BYTES, line

        # A non-active workspace is materialised only when asked for.
        loaded = manager.get_workspace(others[0])
        assert loaded is not None
        assert len(loaded.tabs) == 500
        assert materialised[0] == 1000
        assert len(manager.list_workspaces()) == 2
