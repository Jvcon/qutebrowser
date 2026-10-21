# ADR 0009: Workspace portability — import/export and directory-sync friendliness

## Status

**Accepted** — supersedes the transport choice in issue #27 (VP-A3).

## Context

Issue #27 originally specified "cross-device / cross-window Group sync" with
**`chrome.storage.sync`** as the MVP transport.

`chrome.storage.sync` is a **Chrome extension API**. qutebrowser is a Qt
desktop application; it has no extension runtime and cannot call it. The
quota semantics in that spec (8 KB per item, 100 KB total, chunking,
`chrome.storage.local` fallback) are likewise extension-specific. As written,
#27 was not implementable in this codebase.

Two further constraints rule out substituting a hosted transport:

1. **Issue #1 excludes it.** The parent spec's *Out of Scope* section lists
   `Cloud sync / team collaboration` under *Phase 1 不做* and
   `跨设备同步` under *Phase 3+ 不做（列入存档）*. Building a hosted sync
   service would unilaterally reverse a scope decision the project already
   made.
2. **There is no network substrate.** `misc/requirements.txt` declares no
   HTTP client. The only available option is `urllib.request` from the
   standard library. Adding a networking stack, a credential store, an auth
   flow, an offline write queue and a consistency protocol is a large,
   security-sensitive surface that this project has so far chosen not to carry.

Meanwhile #1's *Phase 2* scope **does** list **workspace 导入导出**
(import/export). That is the sanctioned way to move workspaces between
machines, and it needs no server.

The user-facing need is real: someone who has curated several hundred tabs
into groups on a work machine wants that curation available on a second
machine. The question this ADR answers is how to deliver that without
building a sync service.

## Decision

Deliver workspace portability as **export/import of a versioned JSON
document**, plus **keeping the on-disk layout clean enough to be carried by a
user-chosen file-sync tool**. No network transport, no server, no credentials.

### 1. A versioned JSON interchange format

`:vp-workspace-export <path>` writes a single self-describing JSON document.
`:vp-workspace-import <path>` reads one back.

The format is **separate from the on-disk persistence format**
(`<data>/profiles/default/workspaces/<uuid>.yml`). They have different jobs:
the YAML is a private, machine-written cache whose shape may change freely;
the JSON is a user-visible interchange artifact that must be stable,
versioned, and diffable. Keeping them separate means a persistence refactor
never breaks an export someone is holding.

The document carries a `schema_version` integer. Importers accept any version
they know how to read and run the stored document forward through a migration
chain to the current version. Unknown *newer* versions are rejected with a
clear message rather than partially applied.

### 2. URL is the stable cross-machine reference key

`WorkspaceTab.tab_id` is drawn from a **process-local counter**
(`itertools.count(0)` in `qutebrowser/browser/browsertab.py`). Two machines
will therefore assign the same ids to different tabs. Any id is local.

The interchange format consequently omits `tab_id` entirely and re-addresses
entries by **normalized URL**, which is stable across machines, already the
key used for duplicate detection in `reopen_group`, and gives de-duplication
for free. Two entries with the same URL collapse to one on import.

`Workspace.id` and `Group.group_id` are `uuid.uuid4()` and *are* globally
unique, so they are carried across unchanged and keep referential integrity
for `group_id` on each entry.

`parent_id` is a reference to a local `tab_id`, so it is **remapped during
import** through the same URL key. A child whose parent is not present in the
imported document is promoted to a top-level entry rather than dropped.

### 3. Fields deliberately excluded

Machine-local and derived state stays out of the document:

| Object | Excluded | Why |
|---|---|---|
| `Workspace` | `last_active` | Which workspace this machine last showed |
| `Group` | `window_affinity`, `affinity_enabled` | A `window_id` is meaningless on another machine (#25) |
| `WorkspaceTab` | `tab_id`, `open_status` | Local counter; and OPEN/SUSPENDED reflects this machine's live tabs |
| `WorkspaceTab` | `last_fetched`, `fetch_attempts`, `last_error` | Metadata-fetch bookkeeping for this machine |
| `PinnedTab` | `tab_id` | Local counter; re-addressed by URL like entries |

`reading_status` and `done_at` **are** carried: they are user decisions about
the content, not machine state, and losing them would lose the user's triage.

### 4. Import is additive, never destructive

Import adds the document's workspaces and entries to the current state. It
does not delete, rename or overwrite existing local objects.

- An entry whose URL is already present locally is **skipped**, and the count
  of skips is reported.
- A group whose `group_id` already exists locally is reused; entries land in
  it.
- A workspace whose `id` already exists locally is skipped wholesale.

This means importing the same file twice is a no-op, and importing a file
that overlaps existing state cannot destroy work.

### 5. Directory-sync friendliness is a documentation commitment, not a protocol

The existing layout — one YAML file per workspace plus `_state.yml` — is
already legible to Syncthing, Dropbox, git and similar tools. **No locking,
`device_id`, or `modified_at` machinery will be added.**

The documented contract is the single-user one: *run qutebrowser on one
machine at a time per workspace directory; let the sync tool move files
between machines while no instance is writing.* Running two instances against
one synced directory concurrently remains unsupported, exactly as running two
qutebrowser processes on one profile already is.

Rationale: cross-process file locking is inconsistent across
Linux/macOS/Windows and fights the copy semantics of the sync tools this
targets. Detecting a conflict is strictly better than silently merging two
half-written YAML files.

## Consequences

### Positive

- #27 becomes implementable inside the existing architecture, with no new
  dependency and no code path that touches the network.
- The export artifact is inspectable, diffable and hand-editable, so a user
  can fix a mistake without the project shipping an editor.
- Because import is additive and idempotent, the failure mode of "I ran
  import twice" is a message, not data loss.
- Re-addressing by URL means importing two independently curated sets of
  research tabs merges them sensibly instead of producing id collisions.

### Negative

- No automatic propagation: a change on machine A does not appear on machine
  B until the user exports and imports. This is a real loss against the
  original #27 intent, accepted because the alternative was a service.
- URL-keyed merging collapses deliberately duplicated tabs (the same page
  open twice in different groups becomes one entry). Accepted as the
  desirable behaviour for curated research collections, but it is a
  semantic loss that a user with a genuine duplicate must be told about in
  the import report.
- `parent_id` chains that cross the boundary of what was exported cannot be
  reconstructed; orphaned children are promoted to top level.
- If a future requirement genuinely needs automatic multi-device
  propagation, it requires first amending #1's *Out of Scope* section. That
  is a deliberate gate, not an oversight.

## Alternatives considered

**A hosted relay service.** Rejected: directly contradicts #1's *Out of
Scope*, requires credentials and a data-protection position the project has
not taken.

**User-supplied WebDAV / S3 endpoint.** Rejected for now: it needs
credentials, retry and offline-queue handling, and a consistency protocol —
the highest-risk class of bug, in the area where users lose their own
research. Revivable later without changing the interchange format decided
here, since the JSON document is transport-agnostic.

**HTML bookmarks as the interchange format.** Rejected: expresses bookmarks
only. It cannot carry groups, `reading_status`, `folded` state or
`parent_id`, which are most of what this feature is for.

**YAML as the interchange format.** Rejected: would couple the user-visible
artifact to the private persistence format, which is deliberately
reformatted freely; and carries no schema-version discipline.

**Conflict detection via `device_id` + `modified_at`.** Rejected: detects
that a conflict happened but cannot resolve it, so it converts silent loss
into a warning without preventing the loss.

## References

- Issue #27 — VP-A3 (the spec this ADR revises)
- Issue #1 — parent spec; *Phase 2* lists workspace import/export, *Out of
  Scope* excludes cross-device sync
- ADR 0008 — vertical panel architecture
- Issue #25 — VP-A1 window affinity, whose `window_affinity` is excluded here
  as machine-local

## Implementation status

- Export (`:vp-workspace-export <path>`): implemented, see #27.
- Import (`:vp-workspace-import <path>`): not yet implemented; same issue
  tracks the next PR.

### Implementation notes

Carry / exclude decisions made during implementation of the export half
that are **not** in the table above:

- **`WorkspaceTab.favicon`** is carried (base64 ASCII string already in
  the model, so the cost is a few KB per document) so an imported
  document does not have to re-fetch site icons just to render the
  panel. Symmetric with how `PinnedTab.favicon` is already carried.
- **`pinned`** lives at the **document root**, not under each workspace.
  `WorkspaceManager.list_pinned()` is profile-scoped, and a per-workspace
  copy would duplicate the list N times and force the importer to URL-
  dedup just to undo that duplication.
- **Standalone tabs** (`group_id is None`) are emitted in a workspace-
  level `entries` list. The original issue table only describes the
  per-group `entries` key, but dropping them would silently lose data.

### Known limitations (tracked for follow-ups)

- `Workspace.created_at` is always `null` in the export today, because
  the model has no such field. Tracked in #40; will be lifted to the
  model and through the import path together.
