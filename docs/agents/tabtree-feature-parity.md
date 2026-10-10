# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

# TabTree Feature Parity Matrix

**Status**: Tracks TabTree (Phase 2 vertical panel) feature coverage against the
top `TabBar` (`qutebrowser/mainwindow/tabwidget.py`). Updated per slice; each
row names the slice that landed or will land it.

**Legend**: `✓` done · `◐` partial · `✗` not done · `—` N/A

**Slice key**:

- `first-slice` — initial `TabTree` shell, view + model + delegate (commit
  `9f9db33..` line, see `qutebrowser/mainwindow/tabtree.py`).
- `bridge-slice` — `WorkspaceBridge` (`qutebrowser/mainwindow/workspacebridge.py`)
  wired `TabbedBrowser` → `WorkspaceManager` + `TabTreeModel`.
- `superset-slice` — TabTree parity slice: active highlight, J/K nav, hover ×,
  audio toggle, close/activate via host.
- `pin-slice` — pin-toggle affinity: the `P` glyph column (always visible when
  pinned, hover-only otherwise) + click-to-flip via the bridge.
- `pending` — scheduled, slice TBD.
- `v2` — planned but no slice on the roadmap yet.

## 1. Display

| Capability                            | TabBar | TabTree  | Slice          |
|---------------------------------------|--------|----------|----------------|
| Show tab title (right-elided)         | ✓      | ✓        | first-slice    |
| Show 16×16 favicon                    | ✓      | ✓        | first-slice    |
| Show mute indicator                   | ✓ (`[M]`) | ✓ (♪ + dim) | superset-slice |
| Show audible indicator                | ✓ (`[A]`) | ✓ (♪)       | superset-slice |
| Show loading indicator (↻)            | ✓      | ✓        | superset-slice |
| Active-tab highlight                  | ✓      | ✓ (left-edge accent) | superset-slice |

## 2. Interaction

| Capability                            | TabBar | TabTree  | Slice          |
|---------------------------------------|--------|----------|----------------|
| Click row → switch tab                | ✓      | ✓        | superset-slice |
| Close × button on hover               | ✓ (config-driven) | ✓ (mouseover-only) | superset-slice |
| Middle-click close                    | ✓      | ✗        | pending        |
| J/K navigation (in panel)             | —      | ✓ (`keyboard_activated`) | superset-slice |
| `gT` / `Ctrl+Tab` cycling             | ✓      | ✗        | pending        |
| Audio mute toggle (♪ click)           | ✓      | ✓        | superset-slice |
| Pin toggle (P click)                  | ✓      | ✓        | pin-slice       |
| Drag-and-drop reorder                 | ✓      | ✗        | pending        |
| Drag to new window                    | ✓      | ✗        | pending        |

## 3. Right-click context menu

| Capability                            | TabBar | TabTree  | Slice          |
|---------------------------------------|--------|----------|----------------|
| Reload tab                            | ✓      | ✗        | pending        |
| Mute / unmute                         | ✓      | ✗        | pending        |
| Pin / unpin                           | ✓      | ✗        | pending (click-toggle landed in pin-slice) |
| Duplicate tab                         | ✓      | ✗        | pending        |
| Close tab / close others              | ✓      | ✗        | pending        |
| Move to workspace                     | —      | ✗        | pending (workspace model feature) |

## 4. Filter

| Capability                            | TabBar | TabTree  | Slice          |
|---------------------------------------|--------|----------|----------------|
| `/` search-and-filter overlay         | ✗      | ✗        | v2             |
| Filter Pin panel + TabTree together    | ✗      | ✗        | v2             |
| Active workspace tabs always visible  | n/a    | ✗        | v2             |

## 5. Tree-only (TabTree superset over TabBar)

| Capability                            | TabBar | TabTree  | Slice          |
|---------------------------------------|--------|----------|----------------|
| Group headers (foldable)              | ✗      | ✓        | first-slice    |
| Parent-child tab nesting              | ✗      | ✓        | first-slice    |
| Fold chevron (▶ collapsed / ▼ open)   | —      | ✓        | first-slice    |
| Group tab-count badge (`+N`)          | —      | ✓        | first-slice    |
| Empty-state placeholder ("No tabs in this workspace") | — | ✓ | first-slice |
| Active workspace isolation            | —      | ✓        | bridge-slice   |

## 6. Out-of-band scope

| Item                                         | Reason                                                          |
|----------------------------------------------|-----------------------------------------------------------------|
| Removing the top `TabBar`                    | ADR 0008 trajectory is "TabTree replaces TabBar" but removal is opt-in / later. |
| Live favicon bytes from `BrowserTab`         | First slice uses placeholder pixmap; live icon bytes are deferred to v0.2.       |
| Cross-window tab sync                        | Each window has its own `WorkspaceBridge`; cross-window sync is v2.              |
| Suspended-tab indicator (`zZ`)               | No `suspended` signal in `browsertab.py`; ADR spec'd but no backend yet.        |
| Per-workspace `last_active` restore on tab activation | Affects persistence, not UI. Tracked in `workspaces/persistence.py` backlog. |
| Real audio API integration beyond mute       | `audio_toggled` calls `set_muted`; louder/quieter not in scope.                 |

## 7. Acceptance check (per-slice smoke test)

Each slice that touches TabTree must pass these flows against a live
`qutebrowser`:

1. Open a new tab → row appears in the tree.
2. Click a row in the tree → browser switches to that tab.
3. Press `j` / `k` inside the tree → browser cycles tabs (focus stays in tree).
4. Hover a tab row → close × appears.
5. Click close × on a tab → it closes and disappears from the tree.
6. Switch the active workspace → tree redraws to that workspace's tabs.
7. (pin-slice) Click the `P` glyph on a tab → tab becomes pinned / unpinned.

If any of these fails, the slice is incomplete regardless of unit-test pass
status.