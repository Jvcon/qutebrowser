# Upstream Tracking — qutebrowser

Tracking upstream issues that will eventually land changes our
fork's `Workspace` + future `Profile` abstractions need to align to.
This file is the single source of truth for "what to watch and what
to do when each one moves."

## Conventions

- **Status snapshots** are captured at the top of each entry the day
  we last verified it. Run `gh issue view <N> --repo
  qutebrowser/qutebrowser --json state,updatedAt` to refresh.
- **Alignment actions** are concrete code/docs steps to take when an
  issue transitions from `OPEN` → `OPEN with PR linked` → `merged`.
  Tick the box only after the rebase + tests have actually been run.
- **Re-verify quarterly.** Each entry carries a `next-check` date.
  If nothing has moved by then, push the date out and otherwise leave
  the entry alone.

---

## #5935 — Refactor profile initialization

- **URL:** https://github.com/qutebrowser/qutebrowser/issues/5935
- **Last status check:** 2026-10-08 (this commit)
- **State:** OPEN, `priority: 1 - middle`, `component: style / refactoring`
- **Created / last activity:** 2020-12-04 / 2020-12-06 (issue is dormant;
  no linked PRs as of last check)
- **Linked PRs (merged):** #5936 "Profile cleanup 1" (merged 2020-12-07;
  +63 / -60 across 3 files; refactored `webenginesettings.py` to start
  the cleanup that #5935 itself will finish).
- **Why we care:** The-Compiler's draft design in the issue body
  (`ProfileFactory(QObject)` with a `created = pyqtSignal(
  QWebEngineProfile)`) is exactly the abstraction our future `Profile`
  layer needs to plug into. Our `Workspace.profile_name` field will
  resolve to `factory.create(name)`, and our `Profile` class in
  `qutebrowser/workspaces/model.py` will become a thin facade over
  `QWebEngineProfile` plus a name -> profile-id map.

### Alignment plan when #5935 lands

- [ ] Re-read the merged PR diff for the actual factory API; the
  `created` signal may carry a different payload than the issue draft
  suggests.
- [ ] In `qutebrowser/workspaces/manager.py` add a `ProfileManager`
  that wraps the upstream factory; expose `profile_for_workspace(ws)`
  returning the `QWebEngineProfile` whose name matches
  `ws.profile_name`.
- [ ] Wire `webengineprofile.factory.created` to a slot that
  registers the profile in our `ProfileManager.name_to_id` map.
- [ ] Re-run `tests/unit/workspaces/` to confirm
  `Workspace(profile_name=...)` round-trips through `to_dict /
  from_dict` after the upstream rename.
- [ ] If the upstream factory uses a different storage path than
  `<data>/profiles/<name>/` we anticipated, update the
  "Future alignment: named profile" § in `docs/adr/0008-…` and add
  a migration note for any existing on-disk data.

---

## #4102 — Contextual identities / Firefox containers

- **URL:** https://github.com/qutebrowser/qutebrowser/issues/4102
- **Last status check:** 2026-10-08 (this commit)
- **State:** OPEN, `priority: 1 - middle`
- **Created / last activity:** 2018-07-31 / 2024-01-13
- **Why we care:** The-Compiler has stated repeatedly in this thread
  that he wants multi-profile (named) support in upstream, with
  per-window profiles as the first cut. Our `Profile` class is a
  separate abstraction but the data shape (name, path, optional
  parent) overlaps with what this issue will eventually produce. We
  do NOT need to mirror upstream's UX (1 profile = 1 window) — we
  keep "multiple workspaces share a profile" — but the **storage
  shape** must match so data can be cross-loaded.

### Alignment plan when #4102 moves

- [ ] Re-read the issue thread for any newer design notes from
  The-Compiler (e.g. the 2024 follow-ups).
- [ ] If upstream lands a `Profile`-shaped JSON/YAML that resembles
  ours, reconcile field names (we use `name`; upstream has
  historically called this `id` or `key`).
- [ ] Confirm our path layout `<data>/profiles/<name>/{history,
  cookies, cache, ...}` matches what upstream picks. Rename our
  root if it doesn't (with a one-shot migration commit).

---

## Tracking protocol

1. Quarterly: `gh issue view 5935 4102 --repo
   qutebrowser/qutebrowser --json state,updatedAt,closedAt` for
   each. Update the `Last status check` and `next-check` lines above.
2. When GitHub sends a notification (e.g. a linked PR appears),
   refresh the entry immediately and create a `TODO` comment on
   `Jvcon/qutebrowser` issue #11 (the Phase 2 parent) so the next
   agent session picks it up.
3. If both issues are closed-without-merge for >12 months, remove
   the entries and re-evaluate whether the fork needs to ship its
   own `ProfileFactory` rather than waiting.

---

## Why these two and not others

- We deliberately do **not** track #927 (tree style tabs). Our
  `TabTree` lives in our own `QTreeView` and is independent of
  toofar's `tree-tabs-integration` branch. If toofar ever migrates
  from `QTabBar` + ASCII prefixes to `QTreeView`, the fork can
  re-evaluate; until then, no shared code to align.
- We deliberately do **not** track #572 (per-window sessions).
  Our `Workspace` already covers the use case The-Compiler is
  dragging his feet on; tracking is not actionable.
- We do track #5935 + #4102 because they will introduce an
  upstream-supported `Profile` class, and we want our
  `Workspace.profile_name` field to round-trip with it cleanly when
  the time comes.
