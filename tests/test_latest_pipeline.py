"""Freshness is independent of importance, but shares validated summaries."""
from copy import deepcopy
from datetime import timedelta

from digest.common import load_config
from digest.gemini import GeminiError, GeminiAuthenticationError
from digest.network import FetchError
from digest.pipeline import empty_state, run_pipeline
from tests.test_core import FakeGemini, NOW, article
import pytest


def collect(state, directory, items, now=NOW, client=None):
    cfg = load_config()
    cfg.update(fetch_article_body=False, cve_enabled=False, latest_enabled=True)
    client = client or FakeGemini()
    report = run_pipeline(state, directory, now, cfg, web=object(), gemini=client,
        source_loader=lambda *args: (deepcopy(items), [{'name': 'Offline', 'status': 'ok'}]),
        github_loader=lambda *args: ([], {'name': 'Offline GitHub', 'status': 'ok'}))
    return client, report


def test_latest_includes_fresh_articles_excluded_by_importance_and_reuses_cache(tmp_path):
    important = [article(index, NOW - timedelta(hours=8)) for index in range(25)]
    fresh = [article(100 + index, NOW - timedelta(minutes=index)) for index in range(25)]
    state = empty_state()
    client, report = collect(state, tmp_path, important + fresh)
    day = state['days'][NOW.date().isoformat()]
    assert set(day['articles']) == {row['id'] for row in important[:20]}
    assert set(day['latest_articles']) == {row['id'] for row in fresh[:20]}
    assert report['new_count'] == 40 and report['latest_count'] == 20
    assert len(client.summarized) == len(set(client.summarized)) == 40
    assert not state['pending'] and not day['warnings']
    assert all(pick['id'] in day['articles'] for pick in day['hot'])
    assert not set(day['latest_articles']) & {pick['id'] for pick in day['hot']}
    assert client.calls <= 80
    client, report = collect(state, tmp_path, [], NOW.replace(hour=13))
    assert client.summarized == [] and report['new_count'] == 0
    assert len(day['articles']) == len(day['latest_articles']) == 20


def test_shared_latest_and_importance_articles_are_summarized_once(tmp_path):
    items = [article(index, NOW - timedelta(minutes=index)) for index in range(20)]
    state = empty_state()
    client, report = collect(state, tmp_path, items)
    day = state['days'][NOW.date().isoformat()]
    assert set(day['latest_articles']) == set(day['articles'])
    assert report['new_count'] == 20 and len(client.summarized) == 20


def test_latest_failure_preserves_only_valid_previous_latest_cards(tmp_path):
    state = empty_state()
    collect(state, tmp_path, [article(1)])
    previous = deepcopy(state['days'][NOW.date().isoformat()]['latest_articles'])

    class FailingLatest(FakeGemini):
        def request(self, instruction, data, schema, model, **kwargs):
            if set(schema['properties']['picks']['items']['properties']) == {'id', 'related_ids'}:
                raise GeminiError('Offline latest validation failure')
            return super().request(instruction, data, schema, model, **kwargs)

    collect(state, tmp_path, [article(2, NOW.replace(hour=13))], NOW.replace(hour=13), FailingLatest())
    day = state['days'][NOW.date().isoformat()]
    assert day['latest_articles'] == previous and day['latest_curation']['status'] == 'stale'
    assert day['news_curation']['status'] == 'fresh'
    assert any('최신 기사 선별 보류' in warning for warning in day['warnings'])


def test_latest_authentication_failure_stops_without_followup_api_calls(tmp_path):
    class AuthenticationFailure(FakeGemini):
        def request(self, instruction, data, schema, model, **kwargs):
            if set(schema['properties']['picks']['items']['properties']) == {'id', 'related_ids'}:
                self.calls += 1
                raise GeminiAuthenticationError()
            return super().request(instruction, data, schema, model, **kwargs)

    client = AuthenticationFailure()
    with pytest.raises(GeminiAuthenticationError):
        collect(empty_state(), tmp_path, [article(1)], client=client)
    assert client.calls == 2 and not client.summarized


def test_newer_same_title_replaces_older_seen_copy_in_latest(tmp_path):
    old = article(1, NOW - timedelta(hours=2))
    state = empty_state()
    collect(state, tmp_path, [old])
    newer = article(2, NOW.replace(hour=13))
    newer['title_original'] = old['title_original']
    collect(state, tmp_path, [newer], NOW.replace(hour=13))
    day = state['days'][NOW.date().isoformat()]
    assert set(day['latest_articles']) == {newer['id']}
    assert old['id'] not in day['news_candidates']


def test_body_only_publication_enters_latest_in_same_batch_without_second_fetch(tmp_path, monkeypatch):
    important = [article(index, NOW - timedelta(hours=3)) for index in range(25)]
    undated = article(100); undated['published_at'] = None
    fetched = []
    full_body = 'FULL_BODY_MEMORY_ONLY_' * 300

    def prepare(fetcher, item, source, cfg):
        fetched.append(item['id'])
        return {**item, 'published_at': NOW.isoformat() if item['id'] == undated['id'] else item['published_at'],
                'excerpt': full_body}

    monkeypatch.setattr('digest.freshness.prepare_article', prepare)
    monkeypatch.setattr('digest.pipeline.prepare_article', prepare)
    cfg = load_config(); cfg.update(cve_enabled=False, latest_enabled=True, fetch_article_body=True)
    state = empty_state()
    run_pipeline(state, tmp_path, NOW, cfg, web=object(), gemini=FakeGemini(),
        source_loader=lambda *args: (important + [undated], [{'status': 'ok'}]),
        github_loader=lambda *args: ([], {'status': 'ok'}))
    day = state['days'][NOW.date().isoformat()]
    assert undated['id'] in day['latest_articles'] and undated['id'] not in day['articles']
    assert day['latest_articles'][undated['id']]['published_at'] == NOW.isoformat()
    assert fetched.count(undated['id']) == 1
    assert full_body not in (tmp_path / 'state.json').read_text(encoding='utf-8')


def test_failed_metadata_probe_does_not_download_same_body_twice(tmp_path, monkeypatch):
    undated = article(100); undated['published_at'] = None
    fetched = []

    def prepare(*args):
        fetched.append(args[1]['id'])
        raise FetchError('Offline insufficient evidence')

    monkeypatch.setattr('digest.freshness.prepare_article', prepare)
    monkeypatch.setattr('digest.pipeline.prepare_article', prepare)
    cfg = load_config(); cfg.update(cve_enabled=False, fetch_article_body=True)
    state = empty_state()
    run_pipeline(state, tmp_path, NOW, cfg, web=object(), gemini=FakeGemini(),
        source_loader=lambda *args: ([undated], [{'status': 'ok'}]),
        github_loader=lambda *args: ([], {'status': 'ok'}))
    assert fetched == [undated['id']]
    assert undated['id'] in state['pending']
