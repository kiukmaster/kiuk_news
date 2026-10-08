"""Bounded publication metadata probes for undated publisher listings."""
from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta

from .common import KST, parse_date
from .network import FetchError
from .sources import in_news_window, prepare_article

PUBLICATION_PROBE_LIMIT = 20
PUBLICATION_PROBE_COOLDOWN = timedelta(hours=6)


def _probe_queue(pool: dict, now: datetime, by_source: dict) -> list[tuple[str, dict]]:
    groups = defaultdict(deque)
    oldest = datetime.min.replace(tzinfo=KST)
    candidates = []
    for ident, item in pool.items():
        if item.get('kind') not in ('article', 'event') or parse_date(item.get('published_at')):
            continue
        source = by_source.get(item.get('source_id'))
        if not source or not source.get('enabled', True) or not source.get('fetch_body', True):
            continue
        checked = parse_date(item.get('publication_checked_at'))
        if checked is not None and now - checked < PUBLICATION_PROBE_COOLDOWN:
            continue
        candidates.append((checked or oldest, ident, item))
    # An unchecked item takes precedence; within a source, retry the oldest
    # check first. Alternate publishers to keep abundant lists from starving
    # the university event sources in this small network allowance.
    for _, ident, item in sorted(candidates, key=lambda row: (row[0], row[1])):
        groups[item['source_id']].append((ident, item))
    queued = []
    while groups:
        for source_id in list(groups):
            queued.append(groups[source_id].popleft())
            if not groups[source_id]:
                del groups[source_id]
    return queued


def probe_publications(pool: dict, now: datetime, cfg: dict, fetcher, by_source: dict) -> dict:
    """Resolve at most twenty unknown dates; keep prepared bodies only in RAM.

    The pool gains only a validated publication timestamp and bounded check
    metadata. Unknown dates remain unknown, and known dates are never replaced
    by observation times. The caller can reuse returned evidence for summaries
    without downloading the same selected body twice in this run.
    """
    if not cfg.get('fetch_article_body', False):
        return {}
    limit = max(0, min(PUBLICATION_PROBE_LIMIT,
                       int(cfg.get('publication_probe_max_items', PUBLICATION_PROBE_LIMIT))))
    if limit == 0:
        return {}
    now = now.astimezone(KST)
    prepared_cache = {}
    for ident, item in _probe_queue(pool, now, by_source)[:limit]:
        item['publication_checked_at'] = now.isoformat()
        item['publication_check_status'] = 'unavailable'
        item['published_at'] = None
        # A malformed feed timestamp is not a known publication date. Clearing
        # it in this temporary input allows the publisher's body to supply one.
        prepared_input = {**item, 'published_at': None}
        prepared_input.setdefault('excerpt', '')
        prepared_input.setdefault('title_original', item.get('title_ko', ''))
        try:
            prepared = prepare_article(fetcher, prepared_input, by_source[item['source_id']], cfg)
        except FetchError:
            # Provider messages can contain response content. Persist only the
            # fixed status, then allow another attempt after the cooldown.
            continue
        published = parse_date(prepared.get('published_at'))
        if published is not None:
            item['published_at'] = published.isoformat()
            item['publication_check_status'] = 'available'
            prepared['published_at'] = published.isoformat()
            if not in_news_window(item, now, cfg):
                pool.pop(ident, None)
                continue
        else:
            item['published_at'] = None
            prepared['published_at'] = None
        prepared['publication_check_status'] = item['publication_check_status']
        prepared_cache[ident] = prepared
    return prepared_cache
