"""Offline probes must be bounded, truthful, and reusable within one batch."""
from copy import deepcopy
from datetime import timedelta
from unittest.mock import Mock

import pytest

from digest.common import load_config
from digest.freshness import probe_publications
from digest.network import FetchError
from tests.test_core import NOW, article


SOURCE = {'id': 'aitimes', 'region': 'KR', 'hosts': ['example.com'], 'enabled': True}


def config(**overrides):
    return {**load_config(), 'fetch_article_body': True, **overrides}


def undated(index, source_id='aitimes', kind='article'):
    row = article(index)
    row.update(published_at=None, source_id=source_id, kind=kind)
    return row


def test_probe_discovers_real_publication_and_preserves_body_in_memory_only():
    item = undated(1)
    original_excerpt = item['excerpt']
    stamp = NOW - timedelta(minutes=10)
    html = ('<meta property="article:published_time" content="' + stamp.isoformat() + '">'
            '<article>' + 'PRIVATE_BODY_EVIDENCE ' * 120 + '</article>')
    fetcher = Mock(get=Mock(return_value=(html.encode(), 'text/html', item['url'])))
    pool = {item['id']: item}
    cached = probe_publications(pool, NOW, config(), fetcher, {'aitimes': SOURCE})
    assert item['published_at'] == stamp.isoformat()
    assert item['publication_check_status'] == 'available'
    assert item['publication_checked_at'] == NOW.isoformat()
    assert item['excerpt'] == original_excerpt
    assert 'PRIVATE_BODY_EVIDENCE' not in str(pool)
    assert 'PRIVATE_BODY_EVIDENCE' in cached[item['id']]['excerpt']
    assert cached[item['id']]['evidence_kind'] == 'body_excerpt'
    assert fetcher.get.call_count == 1
    assert probe_publications(pool, NOW, config(), fetcher, {'aitimes': SOURCE}) == {}
    assert fetcher.get.call_count == 1


@pytest.mark.parametrize('stamp', [NOW - timedelta(days=2), NOW + timedelta(seconds=1)])
def test_outside_publication_window_is_removed_without_caching(stamp):
    item = undated(1)
    html = ('<meta name="pub_date" content="' + stamp.isoformat() + '">'
            '<article>' + '검증용 본문입니다. ' * 20 + '</article>')
    fetcher = Mock(get=Mock(return_value=(html.encode(), 'text/html', item['url'])))
    pool = {item['id']: item}
    assert probe_publications(pool, NOW, config(), fetcher, {'aitimes': SOURCE}) == {}
    assert not pool


def test_unknown_body_date_stays_unknown_and_retries_after_six_hours():
    item = undated(1)
    html = ('<article>' + '발행 시각이 없는 검증용 본문입니다. ' * 20 + '</article>').encode()
    fetcher = Mock(get=Mock(return_value=(html, 'text/html', item['url'])))
    pool = {item['id']: item}
    cached = probe_publications(pool, NOW, config(), fetcher, {'aitimes': SOURCE})
    assert cached[item['id']]['published_at'] is None
    assert item['published_at'] is None and item['publication_check_status'] == 'unavailable'
    assert probe_publications(pool, NOW + timedelta(hours=6, seconds=-1), config(), fetcher,
                              {'aitimes': SOURCE}) == {}
    assert fetcher.get.call_count == 1
    assert item['publication_checked_at'] == NOW.isoformat()
    assert probe_publications(pool, NOW + timedelta(hours=6), config(), fetcher,
                              {'aitimes': SOURCE})
    assert fetcher.get.call_count == 2
    assert item['published_at'] is None


def test_fetch_failure_persists_only_fixed_metadata_not_provider_message(monkeypatch):
    item = undated(1)
    prepare = Mock(side_effect=FetchError('PRIVATE_PROVIDER_CONTENT credential=secret'))
    monkeypatch.setattr('digest.freshness.prepare_article', prepare)
    pool = {item['id']: item}
    assert probe_publications(pool, NOW, config(), object(), {'aitimes': SOURCE}) == {}
    assert item['publication_check_status'] == 'unavailable'
    assert item['published_at'] is None and 'PRIVATE_PROVIDER_CONTENT' not in str(pool)
    assert 'credential=secret' not in str(pool)
    assert prepare.call_count == 1


@pytest.mark.parametrize('options', [{'fetch_article_body': False}, {'publication_probe_max_items': 0}])
def test_disabled_probe_does_not_touch_pool_or_fetch(monkeypatch, options):
    item = undated(1)
    pool = {item['id']: item}
    before = deepcopy(pool)
    prepare = Mock(side_effect=AssertionError('No body fetch expected'))
    monkeypatch.setattr('digest.freshness.prepare_article', prepare)
    assert probe_publications(pool, NOW, config(**options), object(), {'aitimes': SOURCE}) == {}
    assert pool == before and not prepare.called


def test_known_publication_and_non_news_or_unavailable_sources_are_skipped(monkeypatch):
    rows = [article(1), undated(2, kind='paper'), undated(3, kind='github'),
            undated(4, source_id='disabled'), undated(5, source_id='no-body'),
            undated(6, source_id='missing')]
    pool = {row['id']: row for row in rows}
    before = deepcopy(pool)
    sources = {'aitimes': SOURCE, 'disabled': {**SOURCE, 'enabled': False},
               'no-body': {**SOURCE, 'fetch_body': False}}
    prepare = Mock(side_effect=AssertionError('Known dates must not be reinterpreted'))
    monkeypatch.setattr('digest.freshness.prepare_article', prepare)
    assert probe_publications(pool, NOW, config(), object(), sources) == {}
    assert pool == before and not prepare.called


@pytest.mark.parametrize('configured, expected', [(200, 20), (3, 3)])
def test_probe_limit_and_publishers_receive_fair_turns(monkeypatch, configured, expected):
    rows = [undated(index) for index in range(30)] + [undated(999, source_id='events', kind='event')]
    pool = {row['id']: row for row in rows}
    prepare = Mock(side_effect=lambda fetcher, item, source, cfg: dict(item))
    monkeypatch.setattr('digest.freshness.prepare_article', prepare)
    cached = probe_publications(pool, NOW, config(publication_probe_max_items=configured),
                                object(), {'aitimes': SOURCE, 'events': SOURCE})
    assert len(cached) == prepare.call_count == expected
    first_two = [call.args[1]['source_id'] for call in prepare.call_args_list[:2]]
    assert set(first_two) == {'aitimes', 'events'}
    assert sum('publication_checked_at' in row for row in pool.values()) == expected


def test_unchecked_precedes_oldest_retry_in_a_source(monkeypatch):
    unchecked, old, recent = [undated(index) for index in range(3)]
    old['publication_checked_at'] = (NOW - timedelta(hours=8)).isoformat()
    recent['publication_checked_at'] = (NOW - timedelta(hours=7)).isoformat()
    pool = {row['id']: row for row in [recent, old, unchecked]}
    prepare = Mock(side_effect=lambda fetcher, item, source, cfg: dict(item))
    monkeypatch.setattr('digest.freshness.prepare_article', prepare)
    probe_publications(pool, NOW, config(publication_probe_max_items=2), object(), {'aitimes': SOURCE})
    assert [call.args[1]['id'] for call in prepare.call_args_list] == [unchecked['id'], old['id']]
    assert recent['publication_checked_at'] == (NOW - timedelta(hours=7)).isoformat()


def test_programming_error_is_not_hidden_as_an_unavailable_date(monkeypatch):
    item = undated(1)
    monkeypatch.setattr('digest.freshness.prepare_article', Mock(side_effect=ValueError('logic defect')))
    with pytest.raises(ValueError, match='logic defect'):
        probe_publications({item['id']: item}, NOW, config(), object(), {'aitimes': SOURCE})


def test_legacy_public_card_without_raw_metadata_uses_actual_body_evidence():
    item = undated(1)
    item['title_ko'] = item.pop('title_original')
    item.pop('excerpt')
    item['summary_ko'] = 'CACHED_SUMMARY_MUST_NOT_BECOME_ORIGINAL_EVIDENCE'
    html = ('<meta name="pub_date" content="' + NOW.isoformat() + '">'
            '<article>' + '실제 원문 본문 근거입니다. ' * 20 + '</article>')
    fetcher = Mock(get=Mock(return_value=(html.encode(), 'text/html', item['url'])))
    pool = {item['id']: item}
    cached = probe_publications(pool, NOW, config(), fetcher, {'aitimes': SOURCE})
    assert cached[item['id']]['title_original'] == item['title_ko']
    assert '실제 원문' in cached[item['id']]['excerpt']
    assert item['summary_ko'] not in cached[item['id']]['excerpt']
    assert 'excerpt' not in item and 'title_original' not in item
    assert item['published_at'] == NOW.isoformat()


def test_metadata_without_body_and_without_original_excerpt_stays_unavailable():
    item = undated(1)
    item.pop('excerpt')
    html = ('<meta name="pub_date" content="' + NOW.isoformat() + '">').encode()
    fetcher = Mock(get=Mock(return_value=(html, 'text/html', item['url'])))
    pool = {item['id']: item}
    assert probe_publications(pool, NOW, config(), fetcher, {'aitimes': SOURCE}) == {}
    assert item['published_at'] is None and item['publication_check_status'] == 'unavailable'


def test_malformed_feed_date_is_unknown_and_can_be_resolved_from_body():
    item = undated(1)
    item['published_at'] = 'not-a-publication-date'
    html = ('<meta property="article:published_time" content="' + NOW.isoformat() + '">'
            '<article>' + '실제 본문 발행일 확인을 위한 근거입니다. ' * 20 + '</article>')
    fetcher = Mock(get=Mock(return_value=(html.encode(), 'text/html', item['url'])))
    pool = {item['id']: item}
    assert probe_publications(pool, NOW, config(), fetcher, {'aitimes': SOURCE})
    assert item['published_at'] == NOW.isoformat()
