# SPDX-FileCopyrightText: Freya Bruhin (The Compiler) <mail@qutebrowser.org>
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Pure keyword-search logic for the vertical workspace panel (VP-P5).

The search is deliberately simple: every query is split on whitespace, all
terms are lower-cased and each term has to match at least one field of an
entry (AND semantics across terms).  No tokenisation or stemming is done, so
mixed CJK/English input works out of the box via plain substring matching.

This module has no Qt dependency and emits no signals; the panel widget is
responsible for turning :class:`SearchHit` objects into model rows and for
triggering the click-to-jump action.
"""

from __future__ import annotations

import dataclasses
import datetime
from typing import Optional

from qutebrowser.workspaces.model import Group, Workspace, WorkspaceTab


@dataclasses.dataclass(frozen=True)
class SearchHit:

    """A single matching workspace entry and its ranking metadata.

    Attributes:
        entry: The matching :class:`WorkspaceTab`.
        group: The owning group, or ``None`` for a standalone entry.
        score: Relevance score; higher is better.
    """

    entry: WorkspaceTab
    group: Optional[Group]
    score: int


def parse_query(q: str) -> list[str]:
    """Split *q* on whitespace, lower-case the parts and drop empties."""
    return [term for term in q.lower().split() if term]


def _created_at_key(entry: WorkspaceTab) -> float:
    """Return a sortable timestamp for *entry*'s ``created_at``.

    Entries with a missing or malformed timestamp sort as the oldest.
    """
    raw = entry.created_at
    if not raw:
        return 0.0
    try:
        parsed = datetime.datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return 0.0
    try:
        return parsed.timestamp()
    except (OverflowError, OSError, ValueError):
        return 0.0


def score_entry(
        entry: WorkspaceTab,
        group: Optional[Group],
        terms: list[str],
) -> Optional[int]:
    """Return a relevance score for *entry*, or ``None`` if it does not match.

    Every term has to match at least one field (title, description, source,
    URL or group name); the per-term scores are summed:

    * title starts with the term: ``+100``
    * title contains the term (but does not start with it): ``+50``
    * each other field containing the term: ``+20``
    * the group name containing the term: ``+15``

    An empty *terms* list scores every entry as ``0`` (the default,
    unfiltered view).
    """
    if not terms:
        return 0

    title = (entry.title or '').lower()
    description = (entry.description or '').lower()
    source = (entry.source or '').lower()
    url = (entry.url or '').lower()
    group_name = (group.name or '').lower() if group is not None else ''

    score = 0
    for term in terms:
        matched = False
        if title.startswith(term):
            score += 100
            matched = True
        elif term in title:
            score += 50
            matched = True
        for field in (description, source, url):
            if term and term in field:
                score += 20
                matched = True
        if term and term in group_name:
            score += 15
            matched = True
        if not matched:
            return None
    return score


def search_workspace(
        workspace: Workspace,
        terms: list[str],
) -> list[SearchHit]:
    """Score every entry in *workspace* and return the sorted matches.

    Results are ordered by descending score, with newer entries first as the
    tie-breaker.  Standalone entries are included with ``group=None`` so the
    panel can keep rendering them alongside grouped hits.
    """
    groups_by_id = {group.group_id: group for group in workspace.groups}
    hits: list[SearchHit] = []
    for entry in workspace.tabs:
        group = groups_by_id.get(entry.group_id)
        score = score_entry(entry, group, terms)
        if score is None:
            continue
        hits.append(SearchHit(entry=entry, group=group, score=score))

    hits.sort(key=lambda hit: (-hit.score, -_created_at_key(hit.entry)))
    return hits
