# ADR 0008: PIN panel, TabTree, and WorkspaceIndicator for the Vertical Panel

## Status

**Accepted** — architecture ratified; the one remaining open decision
(parent-child auto vs explicit) is a minor UX question that does not block
implementation.

## Context

Issue #1 introduces a 220 px-wide vertical workspace panel. This ADR records
its visual design: four stacked permanent sections, a transient Search & Filter
row, the widget for each section, and the details that bind them together.

The panel is organized as:

1. **PIN panel** — app-wide (cross-workspace) pinned tabs, rendered as `icon` or `line`
   style via `pinned_tabs.style`. The two styles are alternative renderings of
   the same set, **not** co-displayed.
2. **TabTree** — a `QTreeView` styled as a tab bar for the *active* workspace's
   tabs, with optional groups and parent-child nesting. Workspace selection
   lives outside the tree, in the WorkspaceIndicator.
3. **WorkspaceIndicator** — workspace switching at the bottom, rendered as a
   `QToolBar` of checkable workspace buttons.

A transient **Search & Filter** row opens at the top of the TabTree on `/` and
closes on `Esc` or when cleared. It filters both the PIN panel and the TabTree
by title and URL while always keeping the active workspace's tabs visible.

Pinned tabs do **not** appear inside the TabTree; workspace tabs do **not**
appear inside the PIN panel. The two views are disjoint by construction.

## Decision

Use a 220 px-wide vertical column with **four stacked permanent sections**, top
to bottom:

1. **PIN panel** — one of `icon` or `line` style, driven by config.
2. **TabTree** — `QTreeView` styled as a tab bar for the active workspace.
3. **WorkspaceIndicator** — `QToolBar` with checkable workspace buttons.

A **transient filter row** appears at the top of the TabTree when the user
presses `/`, and disappears on `Esc` or when the field is cleared.

### Rationale

- `QTreeView` gives us free keyboard navigation, focus handling, scrolling,
  drag-and-drop, expand/collapse animation, and an AT-SPI tree role.
- qutebrowser already uses `QTreeView` for the completion widget and the prompt
  file picker, so the team already owns the delegate/role patterns.
- Separating workspace switching (WorkspaceIndicator) from tab presentation
  (TabTree) makes the active workspace explicit and shrinks the TabTree to the
  one workspace the user is currently working in.
- Groups are **opt-in**: a fresh workspace starts with no groups, only
  standalone tabs. This avoids forcing a new abstraction on users who only need
  parent-child tab chains.
- Pinned tabs are app-wide (cross-workspace, per-profile); giving them their
  own compact panel keeps them accessible without duplicating them into
  every workspace's tab list.
- A transient `QLineEdit` filter, opened with `/`, searches both PIN and
  TabTree so the user can reach any pinned or workspace tab with one
  keystroke. It disappears when inactive, keeping the default panel compact.

### Why not a fully custom `QAbstractItemView`

| Concern | QTreeView + delegate | Fully custom view |
|---|---|---|
| Keyboard nav | Free | Re-implement arrow handling, home/end, type-ahead |
| Scroll/focus | Free | Manual viewport, focus rect, scrollbar sync |
| Drag & drop | Free (`startDrag`, `dropEvent`) | Re-implement mime, indicators, rubber-band |
| Screen reader | `AccessibleRole.Tree` + model roles | Hand-roll AT-SPI2 contract |
| Visual chrome | Delegated paint | Pixel-perfect but expensive |

For a first version we keep the native skeleton and skin it.

## Section specs

### 1. PIN panel

Config-driven widget. Controlled by `pinned_tabs.style = icon | line`.
Default is `icon`. The value persists in config; there is no GUI control or
right-click submenu for toggling it. `:pintab-style-toggle` flips the setting
at runtime (see § Decisions).

**Icon mode** (`icon`):

- Widget: `QListView` in `IconMode`.
- `Flow.LeftToRight`, `ResizeMode.Adjust`.
- Cell size: 24 × 24 px favicon, 4 px padding on all sides.
- Layout: 2 rows × 3 columns = 6 slots visible without scrolling; scrolls if
  more pinned tabs exist.
- Section height: ~54 px (two 32 px rows minus overlap at the inner borders).
- Empty pinned list → section collapses to 0 px.

**Line mode** (`line`):

- Widget: `QListView` in default list mode.
- Row height: 22 px.
- Each row shows: 16 × 16 favicon + elided title + close `×` on hover.
- Section height: 22 px × row count; scrolls after a sensible maximum (e.g.
  8 rows, ~176 px).
- Empty pinned list → section collapses to 0 px.

**Interaction**:

- Left click activates the pinned tab in the current workspace (creates or
  raises the corresponding tab).
- Middle click closes the pinned tab.
- Right click opens a context menu: close, unpin, move to workspace.
- Long-press shows a tooltip with full title + URL.

**Scope**: pinned tabs are **app-wide within a profile** (i.e. they cross
workspace boundaries inside the same profile, but are isolated to the
profile). They are visible in every workspace of the active profile and
persist at `<data>/profiles/<name>/workspaces/pinned.yml`, not in any
per-workspace YAML. See § "Storage layout" for the full tree.

### 2. Search & Filter (transient)

The filter is hidden when inactive. Pressing `/` inserts a 28 px-high
`QLineEdit` at the top of the TabTree with placeholder `/ search...`; the tree
shifts down and collapses back when the row closes (`Esc` or cleared field).

- Filters both PIN panel and TabTree by title and URL substring.
- Active workspace rows always remain visible, even when non-matching.
- Matching text is underlined with `accent = colors.tabs.indicator.start`.
- Pressing `/` while inactive inserts and focuses the row; pressing `/` again
  while active is a no-op.

### 3. TabTree

A `QTreeView` styled as a tab bar for the active workspace's tabs. Groups are
optional: they appear only when the user has created at least one group in the
workspace. With no groups, the tree is a flat (but parent-child nestable) list
of standalone tabs.

Rendered by `TabTreeItemDelegate`:

- **Group header rows** (24 px high) — visible only if `len(groups) > 0`:
  plain group name (no "Group:" prefix), leftmost fold chevron, right-aligned
  tab-count badge (e.g. `+3`, dimmed). Empty groups show `+0` and an unfilled
  chevron.
- **Tab rows** (22 px high) — under a group or standalone:
  under a group the row is indented at the group's depth; standalone tabs sit
  at the top level. Parent tabs add further indentation for descendants. Each
  row shows: fold chevron (only for parent tabs), favicon (16 × 16), elided
  title, and status strip (`↻` loading, `♪` audio, `P` at 0 % opacity, `zZ`
  suspended).

`TabTreeItemDelegate` overrides:

- `sizeHint(option, index)` — 24 px for group headers, 22 px for tabs;
  `setUniformRowHeights(False)` because heights vary.
- `paint(painter, option, index)` — row background, focus ring (keyboard only),
  fold chevron, favicon, elided label, then badge (group) or status strip (tab).
- `editorEvent(event, model, option, index)` — chevron clicks toggle fold
  (group or parent tab), audio icon toggles mute, row body activates, hover
  close `×` closes the tab.
- `helpEvent` — group tooltips show name + tab count; tab tooltips show title +
  URL.

View configuration:

```python
self.setHeaderHidden(True)
self.setIndentation(18)                  # nesting depth for tabs under groups / parent tabs
self.setUniformRowHeights(False)         # 24 vs 22 px rows
self.setAnimated(True)
self.setSelectionBehavior(QAbstractItemView.SelectRows)
self.setSelectionMode(QAbstractItemView.SingleSelection)
self.setEditTriggers(QAbstractItemView.NoEditTriggers)
self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
```

Both **groups** (from `auto_group_rules` or manual creation) and **parent-child
chains** (from tab-opening behavior) contribute to the TabTree's nesting. A tab
may be a member of a group AND have a parent tab; both nestings are displayed
simultaneously in the tree. The model's `group_id` and `parent_id` fields are
independent.

### 3a. Vertical Panel state model

The panel's runtime state spans **WorkspaceTab** and **Group** and is split
into two orthogonal axes: a **runtime presence** axis (driven by the browser)
and a **user-applied** axis (driven by the user / policies).

| Concept | Lives on | Values | Source | Reset on restart |
|---|---|---|---|---|
| `open_status` | `WorkspaceTab` | `OPEN` \| `SUSPENDED` | Bridge: `OPEN` while the browser has a live tab; `SUSPENDED` after the tab is closed or the entry is kept. | All entries restored as `SUSPENDED`; first `_register_tab` flips the matching entry to `OPEN`. |
| `reading_status` | `WorkspaceTab` | `FRESH` \| `ACTIVE` \| `DONE` | User command (`:vp-status-set` / `:vp-status-cycle`) or batch (`:vp-batch-status`). | Persisted; survives restart. |
| `created_at` | `WorkspaceTab` | ISO 8601 UTC | `__post_init__` auto-stamp; user-editable? No. | Persisted; survives restart. |
| `Group.state` (derived) | `Group` (property) | `ARCHIVED` \| `UNKNOWN` | Local: `ARCHIVED` if `group.archived` else `UNKNOWN`. The full `OPEN`/`SUSPENDED`/`ARCHIVED` resolution lives in `WorkspaceManager.group_state(workspace_id, group_id)` so it can read `workspace.tabs`. | Computed at every model rebuild. |
| `pinned` | `Group` | bool | User command. | Persisted. |
| `archived` (group) | `Group` | bool | User command; orthogonal to `Group.state`. | Persisted. |
| `folded` | `Group` | bool | Click / keyboard toggle. | Persisted. |

**Group.state precedence** (resolved by `manager.group_state`):

1. If `group.archived` → `'ARCHIVED'` (archived wins over runtime).
2. Else if at least one member has `open_status == 'OPEN'` → `'OPEN'`.
3. Else → `'SUSPENDED'`.

**Derived counts** (used by the Group header badge):

- `manager.group_open_count(workspace_id, group_id)` — tabs in the group with `open_status == 'OPEN'`.
- `manager.group_total_count(workspace_id, group_id)` — total tabs in the group.

The TabTree renders the badge as `f"{open}/{total}"` (e.g. `"3/12"`,
`"0/5"`) instead of the previous `"+{total}"` form. The `GroupStateRole`
(roles section below) exposes the resolved state string per row; the
delegate uses it to dim SUSPENDED groups and mark ARCHIVED groups.

**Sort order** (configurable via `content.vertical_panel.sort_open_groups_first`,
default `True`):

- Pinned first (always).
- Then OPEN groups.
- Then SUSPENDED / ARCHIVED.
- Within each band, by `group.name`.
- Setting `sort_open_groups_first=False` preserves the original (pinned-first, then
  insertion order).

### 3b. Multi-select & batch ops

The TabTree supports a **selection mode** that overlays a left-margin
checkmark on selected rows. Mouse handling in `TabTreeItemDelegate.editorEvent`
honours `ShiftModifier` (range select via anchor), `ControlModifier` /
`MetaModifier` (toggle single), and plain click (toggle single).
`Ctrl+Space` toggles selection mode itself; the binding is registered in
`bindings.default.workspace_panel:` and removes the previous host-level
`QShortcut` that bypassed the binding system.

The host emits a new `group_focus_requested` signal from the bridge when
the browser's `current_tab_changed` fires, so the active row and its
containing group (if any) auto-locate in the tree (see
`VP-D5 auto-locate on tab switch`).

### 4. WorkspaceIndicator

A `QToolBar` with `QToolButton`s in a single horizontal row.

- Height: 28 px.
- Buttons: 24 × 24 px.
- Icon: workspace monogram (16 × 16 rounded square, deterministic HSL color).
- Tooltip: workspace name.
- Checkable buttons.
- Active workspace: sunken state + accent border (`colors.tabs.indicator.start`).
- Inactive workspace: raised state.
- Right click opens a context menu: rename, archive, delete. Archive
  sets the workspace's `archived: true` field and removes it from the
  indicator while keeping its YAML on disk; a follow-up unarchive action
  restores it.

Workspace switching happens **only** via the WorkspaceIndicator. Clicking a
button switches the active workspace; the TabTree redraws to show that
workspace's tabs. The active workspace is preserved on restart via `last_active`
in workspace YAML.

### 5. Auto-group rules

A config-driven way to assign newly opened tabs to groups automatically based
on URL pattern matching. Rules live in `configdata.yml` under
`auto_group_rules` and are evaluated whenever a tab opens or its URL changes.

> **Naming note**: the key uses underscores (`auto_group_rules`) rather than
> the originally proposed hyphenated `auto-group-rules`. `configfiles.py`
> writes option names verbatim as Python attribute names in generated
> `config.py`, and hyphens are not valid Python identifiers.

**Config schema**:

```yaml
# configdata.yml
auto_group_rules:
  - pattern: <regex>          # URL regex (matched against full URL)
    group: <string>           # group name; auto-created in the workspace if missing
    priority: <int>           # default 0; higher wins; then declaration order
```

**Example**:

```yaml
auto_group_rules:
  - pattern: 'https?://github\.com/.*'
    group: github
    priority: 10
  - pattern: 'https?://(docs|q-stats)\.example\.com/.*'
    group: docs
    priority: 5
```

**Rule evaluation**:

- Evaluate by `priority` descending, then declaration order.
- First match wins; a tab is assigned to at most one group.
- If the named group does not exist in the active workspace's `groups[]`, it is
  created on the fly with `folded: false`.
- Rules set `group_id` only; existing `parent_id` is preserved. The TabTree
  displays both nestings (group container first, then parent-child within).
- Non-matched tabs remain standalone (`group_id = null`).

**Persistence**:

- Matched tabs persist their assigned `group_id` in workspace YAML.
- Auto-created groups are persisted in `groups[]` once created, indistinguishable
  from manually created groups.

**Override**:

- `tab-move-to-group <tab_id> <group_name>` overrides a rule-assigned group.
- The rule does not re-apply unless the tab's URL changes again.
- A rule that names a group that already exists manually uses the existing
  group; rules never overwrite manual placements.

**Command**:

- `auto-group-reapply` — re-runs `auto_group_rules` against every tab in the
  active workspace. Useful after editing the rule list.

## Wireframes

All widths are the full 220 px column.

### a. Default state — icon mode, NO groups, flat tab list (parent-child only)

> *Default state shows no group headers. With `auto_group_rules:` matching tabs
> (e.g., `https://github.com/* → github`), new tabs would auto-assign to a
> `github` group on creation. The wireframe below depicts the no-rules case.*

```text
+-------------------------------------------------------+
|  ▢ ⓖ  ▢ ⓑ  ▢ ⓦ                                       |  54 px  (PIN icon mode)
|  ▢ ⓜ  ▢ ⓡ  ▢ ⓢ                                       |
+-------------------------------------------------------+
|  ▼ GitHub Issue #128                                  |  TabTree (no groups)
|    ○ Pull Request #132                                |
|    ○ Figma mock                                       |
|  ▼ Dashboard                                          |
|    ○ Weekly                                           |
|    ○ Monthly                                          |
|  ○ Settings                              [↻]         |
|  ○ qute://start                                       |
|  ▶ Reviews                                + 2          |
+-------------------------------------------------------+
|        ⏜ product-rd  ⏜ customer-A  ⏜ personal        |  28 px
+-------------------------------------------------------+
```

### b. With groups — 2 user-created groups + a few standalone tabs

```text
+-------------------------------------------------------+
|  ▢ ⓖ  ▢ ⓑ  ▢ ⓦ                                       |  PIN
|  ▢ ⓜ  ▢ ⓡ  ▢ ⓢ                                       |
+-------------------------------------------------------+
|  ▶ client-A docs                              + 4    |  Group 1 (folded)
|  ▼ client-A reports                          + 2      |  Group 2 (expanded)
|    ● Dashboard                                        |
|    ○ Weekly                                           |
|  ○ Settings                                           |  standalone
|  ▶ Reviews                                + 2          |  parent tab standalone
+-------------------------------------------------------+
|        ⏜ product-rd  ⏜ customer-A  ⏜ personal        |
+-------------------------------------------------------+
```

### c. Workspace switch via WorkspaceIndicator

User clicks `customer-A`. TabTree redraws to show customer-A's tabs; PIN panel
is profile-scoped and unchanged. (customer-A here has no groups yet, which is a
valid default TabTree state.)

```text
+-------------------------------------------------------+
|  ▢ ⓖ  ▢ ⓑ  ▢ ⓦ                                       |  PIN
|  ▢ ⓜ  ▢ ⓡ  ▢ ⓢ                                       |
+-------------------------------------------------------+
|  ▼ Ticket #42                                         |
|    ○ Screenshot.png                                   |
|  ○ Reports                                            |
|  ▶ Tickets repo                              + 3      |
+-------------------------------------------------------+
|        ⏜ product-rd  ⏜ customer-A  ⏜ personal        |
|                          ↑ active indicator          |
+-------------------------------------------------------+
```

### d. Filter opened (`/`)

```text
+-------------------------------------------------------+
|  ▢ ⓖ*  ▢ ⓑ  ▢ ⓦ                                       |  * = match (PIN)
|  ▢ ⓜ  ▢ ⓡ  ▢ ⓢ                                       |
+-------------------------------------------------------+
| / github                                              |  transient
+-------------------------------------------------------+
|  ● GitHub Issue #128  ← match                         |
|  ○ GitHub Status    ← match                           |
|  ○ Settings                                           |
|  ○ qute://start                                       |
+-------------------------------------------------------+
|        ⏜ product-rd  ⏜ customer-A  ⏜ personal        |
+-------------------------------------------------------+
```

### e. Group fold/unfold (group 1 expands)

User clicks chevron on `client-A docs`. Its 4 children appear:

```text
+-------------------------------------------------------+
|  ▢ ⓖ  ▢ ⓑ  ▢ ⓦ                                       |  PIN
|  ▢ ⓜ  ▢ ⓡ  ▢ ⓢ                                       |
+-------------------------------------------------------+
|  ▼ client-A docs                              + 4    |  Group 1 expanded
|    ○ Spec                                             |
|    ○ Mockups                                          |
|    ● Overview                                         |
|    ○ API reference                                    |
|  ▼ client-A reports                          + 2      |
|    ● Dashboard                                        |
|    ○ Weekly                                           |
|  ○ Settings                                           |
|  ▶ Reviews                                + 2          |
+-------------------------------------------------------+
|        ⏜ product-rd  ⏜ customer-A  ⏜ personal        |
+-------------------------------------------------------+
```

## Persistence schema (per workspace YAML)

```yaml
# <data>/profiles/default/workspaces/<id>.yml
id: <uuid>
name: <string>
last_active: <iso8601>          # ISO 8601 UTC; stamped on add_workspace and on set_active
color: <hex>                    # optional; overrides the deterministic HSL monogram fill
icon: <emoji | path>            # optional; overrides the letter monogram (emoji glyph or absolute icon path)
archived: false                 # true hides the workspace from WorkspaceIndicator but keeps its data on disk
groups:
  - group_id: <uuid>
    name: github
    folded: false               # auto-created by auto_group_rules
    pinned: false               # optional; pinned groups sort first
    archived: false             # optional; per-group archive (orthogonal to workspace archived)
    created_at: <iso8601>       # optional; auto-stamped in __post_init__
  - group_id: <uuid>
    name: client-A docs
    folded: false
  - group_id: <uuid>
    name: client-A reports
    folded: true
tabs:
  - tab_id: <int>
    parent_id: null
    group_id: null              # standalone top-level tab
    # F1 metadata (backward-compat: older YAML lacks these; from_dict defaults)
    title: ''
    url: ''
    description: ''
    source: ''
    favicon: null               # base64-encoded PNG string, or null
    user_edited: {title: false, description: false, source: false}
    last_fetched: null          # ISO 8601 UTC; stamped on a successful meta fetch
    fetch_attempts: 0
    last_error: null
    # VP-D1 + VP-F3 state (backward-compat: missing → 'SUSPENDED' / 'FRESH' / now())
    open_status: SUSPENDED      # OPEN | SUSPENDED — runtime: tab live in the browser
    reading_status: FRESH       # FRESH | ACTIVE | DONE — user-applied reading marker
    created_at: <iso8601>       # auto-stamped in __post_init__ when missing
  - tab_id: <int>
    parent_id: <int>
    group_id: null              # standalone child of a parent tab
    # ... (same metadata + state fields as above)
  - tab_id: <int>
    parent_id: null
    group_id: <uuid>            # belongs to a group, no parent
    # ...
  - tab_id: <int>
    parent_id: <int>
    group_id: <uuid>            # inside a group AND child of a parent in that group
    # ...
  - tab_id: <int>
    parent_id: null
    group_id: <uuid>            # auto-assigned by auto_group_rules
    # ...
```

Groups are workspace-scoped. Deleting a group nulls its members' `group_id`;
empty groups (`+0`) can be deleted without confirmation. Auto-created groups are
persisted exactly like manually created groups once they exist.

**Backward compatibility**: `WorkspaceTab.from_dict` and `Group.from_dict` use
`.get()` with explicit defaults for every field added after the original
format (`pinned`, `archived`, `created_at`, `title`, `url`, …, `open_status`,
`reading_status`, `created_at`). Older YAML files load without migration.
Adding a new field never requires a version bump or data fix-up.

## Storage layout

All workspace data lives under a per-profile directory tree, designed
to align with the path layout that upstream qutebrowser's planned
[`#5935` ProfileFactory refactor](https://github.com/qutebrowser/qutebrowser/issues/5935)
is expected to settle on (and already seeded by the merged PR
[`#5936`](https://github.com/qutebrowser/qutebrowser/pull/5936)):

```text
<data>/
└── profiles/
    └── <name>/
        └── workspaces/
            ├── pinned.yml              # app-wide pinned tabs within this profile
            ├── <workspace-uuid>.yml    # per-workspace state (one file per workspace)
            └── _state.yml              # active workspace id (single line: active_id: <uuid>)
```

The default profile is named `default`. Multiple workspaces share one
profile (the fork's design is more flexible than upstream's
"1 profile = 1 window" model). Future `QWebEngineProfile` storage
(cookies/history/cache from `#5935`) is expected to live at a
**sibling** of `workspaces/` under the same `profiles/<name>/` root,
not inside it — the namespaces are independent.

### Legacy migration

Pre-Profile data was stored at the basedir root:

```text
<data>/workspaces/<uuid>.yml
<data>/workspaces/_state.yml
<data>/profile/pinned.yml
```

On the first launch after upgrading, `WorkspacePersistence.__init__`
and `PinnedPersistence.__init__` run a one-shot migration (idempotent,
guarded to only run for the default profile, and a no-op when the
caller passed an explicit `base_path` / `path` override). The
migration moves any data found at the legacy locations into
`profiles/default/workspaces/`, deletes the now-empty legacy
directories, and emits a single info log line on the move.

**No-clobber rule**: if both a legacy file and a new file with the
same name exist, the new file wins and the legacy file is dropped
silently. This matches the behaviour we want for users who ran two
builds side-by-side during the migration window.

## Commands (new, in `bindings.default:`)

The registered names are authoritative. qutebrowser's existing reload command
is `reload`; the ADR's earlier tab-prefixed spelling was wrong. `tab-move`,
`tab-close`, `tab-only`, `tab-clone`, and `reload` are existing qutebrowser
commands, not new workspace commands. Binding facts (which mode binds which
command) live in `qutebrowser/config/configdata.yml`; see § "Binding
classification".

| Command | Behavior |
|---|---|
| `workspace-create <name>` | Create a workspace. Refuses a duplicate name (case-insensitive); it becomes active only when no workspace exists yet. |
| `workspace-list` | List all workspaces; `*` marks the active one and archived workspaces carry `(archived)`. |
| `workspace-rename <old> <new>` | Rename a workspace; refuses a name already used by another workspace (case-insensitive). |
| `workspace-delete <name>` | Delete a workspace after confirmation; deleting the active workspace is refused. |
| `workspace-archive <name>` / `workspace-unarchive <name>` | Archive / unarchive a workspace (hidden from the WorkspaceIndicator, kept on disk). |
| `workspace-switch <name>` | Switch the active workspace. |
| `workspace-next` / `workspace-prev` | Cycle to the next / previous non-archived workspace, wrapping around. |
| `vp-workspace-export [<path>]` / `vp-workspace-import [<path>]` | Export all workspaces to a versioned JSON document / import one add-only. (VP-A3, ADR 0009) |
| `vp-group-new <name>` | New Group in the active workspace. Group names are unique across all workspaces (case-insensitive). (VP-F2) |
| `vp-group-rename <target> <new-name>` | Rename a group. |
| `vp-group-pin <target>` / `vp-group-unpin <target>` | Pin / unpin a group so it sorts first. |
| `vp-group-archive <target>` / `vp-group-restore <target>` | Archive / restore a group (hidden from the normal list, kept on disk). |
| `vp-group-delete <target>` | Delete group; tabs become standalone. Empty groups delete immediately; non-empty groups confirm first. (VP-F2) |
| `vp-group-close <target>` | Close every OPEN tab in the group; entries auto-flip to `SUSPENDED`. Group preserved. (VP-D3) |
| `vp-group-reopen <target>` | Reopen every SUSPENDED tab; reuse live tabs by URL, else open a new tab in the current window. (VP-D4) |
| `group-list` | List groups in the active workspace. |
| `tab-move-to-group <tab_id> <group_name>` | Move tab into a group; use `root` for standalone. Overrides auto-group rules. |
| `tab-move <from> <to> [<group_name>]` | Existing move command; can update both `parent_id` and `group_id` in one move. |
| `tab-collect [<group>]` | Collect the current tab into a group; when *group* is omitted, lands in the Inbox default. (VP-F1, VP-F5) |
| `vp-collect-all [<group>]` | Collect every collectable tab in the current window into a group; Inbox default. (VP-F1, VP-F5) |
| `vp-status-set <target> <status>` | Set the reading status (`FRESH`/`ACTIVE`/`DONE`) of a tab. (VP-F3) |
| `vp-status-cycle <target>` | Cycle FRESH → ACTIVE → DONE → FRESH. (VP-F3) |
| `vp-cleanup-done [days]` | Remove DONE+SUSPENDED tabs older than *days* (30–180, default from `content.vertical_panel.auto_cleanup_days`). Silent on N=0. (VP-F3) |
| `vp-suspend-stale [days]` | Flip OPEN tabs marked DONE and untouched for *days* to `SUSPENDED`; the live browser tab is left alone. Default from `content.vertical_panel.auto_suspend_done_days`. |
| `vp-suggestion-stale` | Print the current smart-suggestion list, omitting groups inside the ignore cooldown. |
| `vp-suggestion-ignore` | Mark all currently-suggested groups as ignored for 7 days. |
| `vp-select-mode` | Toggle multi-select mode in the TabTree. (VP-F4) |
| `vp-select-toggle <tab_id>` | Toggle a single tab's selection. (VP-F4) |
| `vp-select-all` / `vp-select-none` | Bulk select. (VP-F4) |
| `vp-batch-move <group>` | Move the selected tabs into a group. (VP-F4) |
| `vp-batch-status <status>` | Apply a reading status to the selected tabs. (VP-F4) |
| `vp-batch-delete` (or `-!` to skip confirm) | Delete the selected tabs. (VP-F4) |
| `vp-batch-open-window` | Open the selected tabs in a single new window; reuse live tabs by URL. (VP-F4) |
| `vp-batch-open-tabs` | Open the selected tabs as new tabs in the current window. (VP-F4) |
| `auto-group-reapply` | Re-run `auto_group_rules` on every tab in the active workspace. |
| `pintab-add <target>` / `pintab-remove <target>` | Pin a tab entry / unpin it without closing the live tab. The PIN panel and TabTree are disjoint. |
| `pintab-move <from> <to>` | Reorder a pinned entry within the PIN panel. |
| `pintab-style-toggle` | Flip `pinned_tabs.style` between `icon` and `line` at runtime; see § Decisions. |
| `panel-filter` | Open the transient Search & Filter row at the top of the TabTree. |
| `vertical-panel-toggle` | Toggle VerticalPanel visibility; no tab/group target. |
| `reload` | Existing qutebrowser reload command; not a new workspace command. |

## Per-decision specifics

### Pinned-tab drop target — **Resolved**

When a pinned tab is dragged from the PIN panel into a TabTree branch:

1. The tab is **unpinned** (removed from the per-profile `pinned.yml`).
2. The tab **becomes a regular tab** in the active workspace (added to
   `<workspace>.yml`'s `tabs[]`).
3. **If the drop position is inside a group**, the tab is re-parented:
   - `group_id` is set to the target group.
   - If the drop is on a specific tab row within the group, `parent_id` is set
     to that tab.
4. **If the drop is outside any group**, the tab is a top-level standalone row
   (`group_id = null`, `parent_id = null`).

Implementation: inspect the drop target's `QModelIndex`; if it carries a
`group_id` data role, the new tab joins that group.

### Workspace monogram

Every WorkspaceIndicator button shows a 16 × 16 px glyph.

**Default**: deterministic letter monogram.

- First Unicode scalar of the workspace name, centered in a 16 × 16 rounded
  square.
- Fill from a user-set `color` field, otherwise a muted deterministic HSL color
  (`H = hash(name) % 360`, `S = 25 %`, `L = 45 %`). Emoji or icon path
  overrides the monogram.

**Contrast**: if a generated color fails against `base`, shift luminance toward
`fg` until it passes WCAG AA (3 : 1 for icons).

HiDPI: render at 2× and let Qt auto-scale via `AA_UseHighDpiPixmaps`; only call
`QPixmap.setDevicePixelRatio` manually if visual tests show artifacts.

### Tab favicon fallback

When a tab has no favicon, the 16 × 16 icon slot shows a **domain initial**
monogram: eTLD+1 initial (`github.com` → "G"), `?` for empty or `qute://*`
URLs, tinted by page theme color when available or neutral gray otherwise. The
monogram cross-fades to the favicon over ~120 ms when one loads.

### Status indicators on a tab row

Status icons are 14 × 14 px, monochrome, and drawn in a single trailing strip;
inactive slots collapse so the text has maximum room.

| Indicator | Glyph | Visual treatment | Click action |
|---|---|---|---|
| Loading | `↻` | Rotating arc, 12 fps, 40 % opacity | None |
| Audio | `♪` | Speaker outline; `♪/` when muted | Mute/unmute |
| Pinned | `P` | 0 % opacity in TabTree | None |
| Suspended | `zZ` | 50 % opacity | None |

**Layout**: fixed-order strip `[loading] [audio] [pinned] [suspended]`, 4 px
gutter, constrained to 56 px. The label elides before the strip is truncated.

### Fold indicator

Use a **custom 9 × 9 px chevron**, not the native `PE_IndicatorBranch` glyph.

- Right-pointing `▶` when collapsed, down-pointing `▼` when expanded.
- 2 px stroke, rounded caps, drawn in the current foreground color.
- Empty groups show an unfilled chevron; parent tabs with no children show no
  chevron but keep the 14 px gutter so labels align.
- Clicking the chevron toggles expand/collapse without activating the row.

### Color scheme

Colors derive from existing config. Panel base = `colors.webpage.bg`;
foreground = `colors.tabs.odd.fg` (fallback because `colors.webpage.fg` does
not exist); accent = `colors.tabs.indicator.start`. Blending is linear RGB.

```
blend(base, overlay, amount) = base * (1 - amount) + overlay * amount
```

| Element | Fill |
|---|---|
| Panel / PIN background | `base` |
| Search field | `blend(base, fg, 0.03)` |
| TabTree row | `blend(base, fg, 0.05)` |
| Hover | `blend(base, fg, 0.07)` |
| Selected | `blend(base, fg, 0.12)` |
| Loading icon | `fg` at 40 % opacity |
| Suspended icon | `fg` at 50 % opacity |
| Search-match underline | `accent` |
| Active WorkspaceIndicator border | `accent` (2 px solid) |

**Contrast**: text ≥ 4.5 : 1, icons ≥ 3 : 1 (WCAG AA). Shift generated
monogram luminance toward `fg` until it passes.

### Focus ring

The focus ring appears only on keyboard focus (`TabFocusReason`,
`BacktabFocusReason`, or shortcut); mouse clicks suppress it.

- **Outer**: 2 px solid `accent`.
- **Inner**: 1 px solid `fg`, inset, so the ring stays visible on any
  background.
- 2 px corner radius to match the rounded workspace monograms.

### Keyboard navigation

```
Tab / Shift+Tab       Cycle focus: webview → PIN panel → TabTree → WorkspaceIndicator → webview
Arrow Down / Up       Move selection to next/previous visible row in TabTree
Arrow Right           Expand collapsed group/parent tab, or move focus to first child
Arrow Left            Collapse expanded group/parent tab, or move focus to parent
Space / Return        Activate selected row (group header: no-op; parent tab: focus child; tab: focus tab)
Ctrl+W                Close selected tab
Shift+Ctrl+W          Close active workspace (prompt if tabs are present)
Shift+F2              Show close-branch hint for the selected tab
/                     Open the filter row at the top of the TabTree and focus it (no-op if already open)
g g                   Move focus to the next group header (or create one if none) — binding details TBD
Esc                   If filter row is open, clear it and remove it; if filter empty and panel focused, return focus to webview
Ctrl+\                Toggle VerticalPanel visibility
```

**Activation**: group header = no-op; parent/tab row = focus tab (switch
workspace first if `parent_id`/`group_id` is foreign); PIN item = activate
pinned tab; WorkspaceIndicator button = switch workspace.

**Close-branch hint** (`Shift+F2`) shows a small overlay:

```
[w] close workspace  customer-A
[t] close tab        Ticket #42
```

Press `w` or `t` to execute; any other key dismisses.

### Panel focus & mode model

The VerticalPanel and the browser area are two **physically separate
panels** (joined by a `QSplitter`). Keyboard focus moves between them via
the standard Qt focus chain, and qutebrowser's binding parser dispatches
keys by `KeyMode`, not by panel. The bridge is a single dedicated mode
that covers the entire VerticalPanel.

**Mode**: `usertypes.KeyMode.workspace_panel` (new in Phase 2).

- `enter(workspace_panel)` runs when Qt focus enters any direct child of
  the `VerticalPanel` root: the `PINPanel`, the `TabTree`, the `FilterRow`
  (when open), and any button in the `WorkspaceIndicator`.
- `leave(workspace_panel)` runs when focus leaves the panel back to the
  webview (or to a transient overlay such as prompt / hint).
- Inside the mode, all panel-state Phase 2 commands bind in
  `bindings.default.workspace_panel:`. The browser area remains in
  `KeyMode.normal`.

**Focus rules**:

- `Tab` / `Shift+Tab` cycles focus within the panel:
  `PINPanel → TabTree → WorkspaceIndicator → PINPanel`. From the last
  WorkspaceIndicator button, `Tab` wraps to the first PINPanel slot.
- `Esc` from `workspace_panel` returns focus to the webview and leaves
  the mode. `Esc` from a transient `prompt` (the filter row) closes the
  filter and returns focus to the TabTree, staying in `workspace_panel`.
- `Ctrl+\` toggles the panel's visibility AND focus state: hidden → visible
  moves focus to whichever child was last focused (or the TabTree if
  first time); visible → hidden returns focus to the webview and leaves
  the mode.
- Mouse clicks on a panel child update the auto-`setFocus` target so
  re-entering the panel after `Ctrl+\` lands on the clicked widget, not
  on the TabTree by default.

**Mode stack**: `workspace_panel` coexists with the existing transient
modes (`hint`, `command`, `prompt`, `caret`, `passthrough`, `insert`).
The `ModeManager._prev_mode` machinery already records
`workspace_panel` as the prev mode for any of these that stack on top, so
`Esc` from a transient restores the panel instead of falling through to
`normal`.

**Visual feedback**: the status bar's mode indicator shows
`-- WORKSPACE --` while in `workspace_panel` (same channel the existing
`-- INSERT --` / `-- CARET --` indicators use). The panel's focus ring
(§ Focus ring) follows the same keyboard-only contract.

### Screen reader contract

The TabTree and PIN panel expose a clean AT-SPI2 contract:

- TabTree `accessibleName` is `"Tabs"`.
- PIN panel `accessibleName` is `"Pinned tabs"`.
- WorkspaceIndicator `accessibleName` is `"Workspaces"`.
- The TabTree role is `QAccessible.Role.Tree`.
- Each row sets:
  - `Qt.ItemDataRole.AccessibleTextRole` for the spoken label.
  - `Qt.ItemDataRole.AccessibleDescriptionRole` for state.
  - `Qt.ItemDataRole.ToolTipRole` as a fallback for older ATs.

**Group row example**:

```
AccessibleText:        "client-A docs"
AccessibleDescription: "group, 4 tabs, collapsed"
```

**Parent tab example**:

```
AccessibleText:        "Dashboard"
AccessibleDescription: "tab, host.com/dashboard, parent, expanded"
```

**Tab row example**:

```
AccessibleText:        "GitHub Issue #128"
AccessibleDescription: "tab, github.com/issues/128, loading, audio playing"
```

When expand/collapse changes, emit `model.dataChanged` for the row index so the
screen reader re-announces the new state.

## Binding classification

All Phase 2 commands and key bindings split into three classes by where
they register in `bindings.default:`.

| Class | Registered in | Example commands | Target semantics |
|---|---|---|---|
| **Active-tab operations** | `bindings.default.normal:` **and** `bindings.default.workspace_panel:` | `pintab-add`, `pintab-remove`, `pintab-move`, `tab-move-to-group`, `tab-move`, `tab-close`, `tab-only`, `tab-clone`, `reload`, `tab-collect`, `vp-status-set`, `vp-status-cycle`, `vp-select-toggle` | In both modes the target is the webview's active tab. Outside selection mode the TabTree's current row is kept identical to it: clicking a row and J/K both switch the webview (J/K keeps focus in the tree), so the two cannot diverge. Selection mode is the exception — see § "Current row vs selected set" below. |
| **Panel-state operations** | `bindings.default.workspace_panel:` only | `workspace-create`, `workspace-list`, `workspace-rename`, `workspace-delete`, `workspace-archive`, `workspace-unarchive`, `workspace-switch`, `workspace-next`, `workspace-prev`, `vp-workspace-export`, `vp-workspace-import`, `pintab-style-toggle`, `vp-group-new`, `vp-group-rename`, `vp-group-pin`, `vp-group-unpin`, `vp-group-archive`, `vp-group-restore`, `vp-group-delete`, `vp-group-close`, `vp-group-reopen`, `group-list`, `vp-collect-all`, `vp-cleanup-done`, `vp-suspend-stale`, `vp-suggestion-stale`, `vp-suggestion-ignore`, `vp-select-mode`, `vp-select-all`, `vp-select-none`, `vp-batch-move`, `vp-batch-status`, `vp-batch-delete`, `vp-batch-open-window`, `vp-batch-open-tabs`, `auto-group-reapply`, `panel-filter` | Operate on the active workspace / selected group / pinned-tab list. Have no meaning from `normal:` because they have no active-tab target. |
| **Global operations** | `bindings.default.normal:` only | `vertical-panel-toggle` | Toggle panel visibility; no tab/group target. |

**Previously unclassified commands.** `workspace-list`, `vp-suspend-stale`,
`vp-suggestion-stale`, and `vp-suggestion-ignore` are **panel-state
operations**. Each reads or mutates the active workspace (or its groups /
suggestions) and has neither an active-tab target nor a global-panel target,
so panel-state is the only fit. None of the four has a default binding today;
they are reachable via `:` from either mode. If a default binding is added, it
belongs in `bindings.default.workspace_panel:`.

**Source of truth.** These class tables are hand-maintained. The authoritative
record of which command is bound in which mode is `bindings.default.normal` and
`bindings.default.workspace_panel` in
`qutebrowser/config/configdata.yml`; re-check both tables whenever a command or
default binding changes.

**Registration rule for Phase 5**: every new command MUST be classified
into exactly one of the three classes above in `configdata.yml`, and
**active-tab operations MUST appear in both `normal:` and
`workspace_panel:`** so the same key works from either focus.

### Current row vs selected set

Outside selection mode, the TabTree's current row
(`QTreeView.currentIndex()`) and the webview's active tab are the same tab.
Clicking a row emits `tabtree.activated` and J/K emits
`tabtree.keyboard_activated`; both call `_activate_tab_id()`, which switches
the webview's active tab (J/K keeps focus in the tree). Outside selection
mode, J/K without selection falls back to `_move_selection()`, another plain
tab switch. Therefore the current row cannot point at a tab other than the
webview's active tab.

`vp-select-mode` is a TabTree-model toggle
(`TabTreeModel._selection_mode`), **not** a qutebrowser `KeyMode`. It does
not affect the webview. Its only effect is to change what Ctrl/Shift-click
and J/K mean in the tree. In selection mode J/K moves the current row and
extends the set without switching the webview, so the current row can then
differ from the webview's active tab. Two independent notions coexist:

- the **current row** (`_view.currentIndex()`), which is always exactly one
  row;
- the **selected set** (`model.selected_tab_ids()`, a `set[int]`), which is
  non-empty only in selection mode and is consumed only by the `vp-batch-*`
  commands.

Enabling selection mode deliberately selects nothing; disabling it clears
the set.

**Decision:** in selection mode, single-target active-tab commands act on
the **current row**, not on the selected set. This is deliberate: the set is
the input for batch operations only, and `vp-batch-*` is the way to act on
it.

## Reconciliation notes — qutebrowser-specific

1. **Optional tab groups** — qutebrowser's tab primitives are individual tabs
   with optional `parent_id`. We are NOT adding a hard "tab group"
   requirement. Groups are an opt-in organizational feature layered on top of
   `parent_id` (a group is a container the user creates; tabs can be moved into
   or out of it). v0.1 ships the model and the commands; group UX (drag tabs
   into a group, fold, count badge) is what the wireframes show.

2. **`QTabBar` → `TabTree` swap**: the existing `TabBar(QTabBar)` subclass in
   `tabwidget.py` is flat. TabTree requires a `QTreeView` styled to look like
   tabs. The custom `paintEvent` + `TabBarStyle` (`QProxyStyle`) and the `[M]` /
   `[A]` title-format injection (`tabwidget.py:39-41`, `:180-188`) are now
   TabTree delegate concerns, not `QTabBar` concerns. Decision: keep the
   `colors.tabs.*` namespace (apply the same odd/even + pinned/selected palette
   convention to TabTree rows), drop the title-format `[M]` / `[A]` glyph
   injection since status comes from `AudioRole` / `LoadingRole` model roles
   consumed by the delegate.

3. **Pinned tabs are per-profile, app-wide** — persistence lives at
   `<data>/profiles/<name>/workspaces/pinned.yml` (see § "Storage
   layout"), separate from per-workspace tab lists in
   `<data>/profiles/<name>/workspaces/<id>.yml`. Schema:

   ```yaml
   # <data>/profiles/default/workspaces/pinned.yml
   pinned:
     - tab_id: <int>
       title: <string>
       url: <string>
       favicon: <base64-png>     # optional, embedded so icon survives broken favicons
       last_active: <iso8601>
   ```

   Pinned tabs are NOT in workspace YAML.

4. **Issue #3 is now obsolete** — its scope (`QTreeView` + parent-child
   persistence for workspaces) is replaced by TabTree (QTreeView for the active
   workspace's tabs) + WorkspaceIndicator. Recommend closing #3 and replacing it
   with a new slice "TabTree + PIN panel" that subsumes both #3 and #8.

5. **`colors.webpage.fg` does not exist** upstream; this ADR uses
   `colors.tabs.odd.fg` as fallback.

6. **`setDevicePixelRatio` is a new pattern** in qutebrowser (no existing usage).
   Default to letting Qt auto-scale via `AA_UseHighDpiPixmaps`; only manual
   `setDevicePixelRatio` if visual tests show artifacts.

7. **Bindings** live in two buckets in `configdata.yml`:
   `bindings.default.normal:` and `bindings.default.workspace_panel:` (the
   latter introduced with `KeyMode.workspace_panel`). Names follow
   `workspace-*` / `pintab-*` / `vp-group-*` / `tab-*` convention; the `vp-`
   prefix is required for group/selection/batch/suggestion commands which
   would otherwise collide with qutebrowser's existing namespaces. The command
   set is expected to cover:

   - `workspace-create`, `workspace-list`, `workspace-rename`,
     `workspace-delete`, `workspace-archive`, `workspace-unarchive`,
     `workspace-switch <name>`, `workspace-next`, `workspace-prev`.
   - `vp-workspace-export`, `vp-workspace-import`.
   - `pintab-add`, `pintab-remove`, `pintab-move`, `pintab-style-toggle`.
   - `vp-group-new`, `vp-group-rename`, `vp-group-pin`, `vp-group-unpin`,
     `vp-group-archive`, `vp-group-restore`, `vp-group-delete`,
     `vp-group-close`, `vp-group-reopen`, `group-list`.
   - `tab-move-to-group`, `tab-move <from> <to> [<group_name>]`,
     `tab-collect`, `vp-collect-all`.
   - Status / cleanup / suggestions: `vp-status-set`, `vp-status-cycle`,
     `vp-cleanup-done`, `vp-suspend-stale`, `vp-suggestion-stale`,
     `vp-suggestion-ignore`.
   - Panel visibility and filter: `vertical-panel-toggle`, `panel-filter`.
   - `reload` is qutebrowser's existing reload command, not a new binding.

8. **VerticalPanel focus & key binding model** — the VerticalPanel and
   the browser area are two `QSplitter`-pinned panels; physical focus is
   Qt's job. Keyboard dispatch is `KeyMode`-based, not panel-based. The
   VerticalPanel owns one mode, `KeyMode.workspace_panel`, entered on
   focus-in to any panel child and left on focus-out. All Phase 2
   commands split into three binding classes (active-tab / panel-state /
   global); see the § "Binding classification" section above for the
   table. The trade-off vs. parser-side focus dispatch is recorded in
   § Decisions (Approach C accepted; Approach B rejected).

## Accessibility tier commitment

**Tier 2** — committed for v0.1.

We will deliver:

- Full keyboard navigation through PIN panel, Search, TabTree, and
  WorkspaceIndicator.
- Visible, high-contrast focus rings that appear only on keyboard focus.
- Screen-reader labels for every row via model roles.
- WCAG AA text contrast (4.5 : 1) and icon contrast (3 : 1) for all shipped
  default themes.

**What Tier 3 would require** (out of scope for v0.1):

- Testing with a physical Braille display and full state verbosity tuning.
- Single-switch control support with adjustable dwell timing.
- Voice-control label registration so the tree rows are reachable by dictation.
- High-contrast / forced-colors theme support beyond the existing qutebrowser
  palette.
- WCAG AAA (7 : 1) contrast for all text.
- Automated a11y regression tests using a tool such as Accerciser or
  `pytest-qt` AT-SPI assertions.

## Decisions

All spike decisions are closed. None remain open before v0.1 implementation.

*Resolved*:

- **Search scope** filters both PIN + TabTree.
- **Groups are opt-in** (no hard requirement).
- **Group semantics** — auto-group via `auto_group_rules` config, while
  parent-child chains also shape the tree; both relations coexist.
- **Parent-child auto vs explicit** — `parent_id` is **auto-set** when a tab
  opens from another tab (link click, `:open`, `window.open`); this preserves
  existing qutebrowser behavior. Drag-to-reparent is a v0.2 feature.
- **Pinned-tab drop target** — unpin + become regular; re-parent if dropped
  inside a group, otherwise top-level standalone.
- **Pin panel collapse on empty** — collapse to 0 px.
- **WorkspaceIndicator active style** — accent border + sunken.
- **Pin style toggle UX** — **`:pintab-style-toggle` stays**, bound to `gS` in
  `bindings.default.workspace_panel:`. It writes `pinned_tabs.style` (the
  single persisted source of truth) between `icon` and `line`; there is no GUI
  widget or right-click submenu. The earlier "config-only" wording was wrong:
  style is config-backed, not config-only. `configdata.yml`'s description
  still says "Style switching is config-only; there is no UI toggle" and should
  be reworded to "no GUI control; `:pintab-style-toggle` switches at runtime"
  in a follow-up (that file is outside this ADR change). Removing the command
  or its binding would be a behaviour change, so it is not done here; the
  wording follow-up is flagged for a human decision.
- **Empty-group deletion** — empty groups (`+0`) are deleted **without
  confirmation**; non-empty groups require a confirm prompt and null the
  members' `group_id`.
- **VerticalPanel focus & binding model** (Approach C) — one
  `KeyMode.workspace_panel` covers the whole panel;
  `bindings.default.normal:` carries the webview's active-tab
  operations, `bindings.default.workspace_panel:` carries the panel-state
  operations plus the same-key-acts-on-selected-row variants of
  active-tab operations. Parser is unchanged; focus dispatch stays with
  Qt. Approach B (parser-side focus dispatch) rejected: ~5× implementation
  cost (parser schema upgrade + index rewrite) for zero UX gain in
  Phase 2's command set. See § "Binding classification" and § "Panel
  focus & mode model" for the rule and the focus protocol.

## References

- Issue #1 — Vertical Workspace (parent spec).
- Issue #3 — original cross-workspace `QTreeView` widget ticket, **now
  obsolete** under this ADR's architecture (TabTree + WorkspaceIndicator).
  Recommend closing #3 and opening a new "TabTree + PIN panel"
  implementation slice that subsumes both #3 and #8.
- Issue #4 — VerticalPanel composition.
- Issue #5 — Workspace focus + last tab restoration.
- Issue #6 — Cross-workspace tab move.
- Issue #7 — SIGKILL crash recovery.
- Issue #8 — UX spike: PIN panel + TabTree + WorkspaceIndicator visual design
  (this ADR).
- `qutebrowser/mainwindow/tabwidget.py` — existing `TabBar` and `TabBarStyle`.
- `qutebrowser/completion/completionwidget.py` and
  `qutebrowser/completion/completiondelegate.py` — prior art for `QTreeView` +
  custom delegate.
- `qutebrowser/mainwindow/prompt.py` — existing `QTreeView` styling patterns.
- `qutebrowser/config/configdata.yml` — default key bindings location.
