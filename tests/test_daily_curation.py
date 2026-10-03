"""Daily quotas and selection-before-fetch integration, with offline Gemini."""
from collections import Counter
from copy import deepcopy

import pytest

from digest.common import load_config
from digest.gemini import GeminiError, GeminiAuthenticationError
from digest.pipeline import empty_state, run_pipeline
from tests.test_core import FakeGemini, NOW, article, run


class LatestFirstGemini(FakeGemini):
    def request(self, instruction, data, schema, model, **kwargs):
        # Simulate a valid model deciding the newly arrived event matters most.
        data = deepcopy(data)
        data['candidates'].reverse()
        return super().request(instruction, data, schema, model, **kwargs)


def test_three_batches_share_daily_quota_and_promote_new_issues(tmp_path):
    state = empty_state()
    original = [article(index) for index in range(40)]
    client = run(state, tmp_path, original)
    assert len(client.summarized) == 20
    assert len(state['days'][NOW.date().isoformat()]['articles']) == 20
    assert not state['pending']  # Unselected candidates are not summary failures.
    later = article(99, NOW.replace(hour=13))
    client = run(state, tmp_path, [later], NOW.replace(hour=13), client=LatestFirstGemini())
    day = state['days'][NOW.date().isoformat()]
    assert len(day['articles']) == 20 and later['id'] in day['articles']
    assert len(client.summarized) <= 20
    run(state, tmp_path, [], NOW.replace(hour=19))
    assert len(day['articles']) == 20
    assert all(pick['id'] in day['articles'] for pick in day['hot'])
    assert day['news_curation']['model'] == 'gemini-3.8-flash'


def test_fetch_and_summary_only_selected_representatives(tmp_path, monkeypatch):
    state, fetched = empty_state(), []
    items = []
    for category, kind in [('ai', 'article'), ('security', 'article'), ('tech', 'paper'),
                           ('event', 'event'), ('github', 'github')]:
        for index in range(25):
            row = article(len(items))
            row.update(category_hint=category, kind=kind, observed_at=NOW.isoformat())
            items.append(row)

    def prepare(fetcher, item, source, cfg):
        fetched.append(item['id'])
        return item

    monkeypatch.setattr('digest.pipeline.prepare_article', prepare)
    cfg = load_config()
    cfg.update(cve_enabled=False)
    client = FakeGemini()
    run_pipeline(state, tmp_path, NOW, cfg, web=object(), gemini=client,
                 source_loader=lambda *args: (items[:-25], []),
                 github_loader=lambda *args: (items[-25:], {'status': 'ok'}))
    day = state['days'][NOW.date().isoformat()]
    assert Counter(row['category'] for row in day['articles'].values()) == dict.fromkeys(
        ['ai', 'security', 'tech', 'event', 'github'], 20)
    assert len(fetched) == 80 and len(client.summarized) == 100
    assert not state['pending']
    assert client.calls <= cfg['max_api_calls_per_run']


def test_reselection_failure_preserves_previous_capped_result(tmp_path):
    state = empty_state()
    run(state, tmp_path, [article(index) for index in range(25)])
    previous = deepcopy(state['days'][NOW.date().isoformat()]['articles'])

    class FailingCuration(FakeGemini):
        def request(self, *args, **kwargs):
            raise GeminiError('테스트 선별 실패')

    run(state, tmp_path, [article(99)], client=FailingCuration())
    day = state['days'][NOW.date().isoformat()]
    assert day['articles'] == previous
    assert day['news_curation']['status'] == 'stale'
    assert state['pending'] and all(pick['id'] in previous for pick in day['hot'])


def test_promoted_exclusions_keep_pending_when_summary_fails(tmp_path):
    state = empty_state()
    run(state, tmp_path, [article(index) for index in range(25)])
    excluded = {ident for ident, record in state['seen'].items() if record.get('excluded_by_curation')}
    assert len(excluded) == 5
    run(state, tmp_path, [], NOW.replace(hour=13), client=LatestFirstGemini(fail_summary=True))
    assert excluded.issubset(state['pending'])
    assert state['last_run']['pending_count'] == 5


def test_curation_authentication_failure_aborts(tmp_path):
    class Unauthorized(FakeGemini):
        def request(self, *args, **kwargs):
            raise GeminiAuthenticationError()

    with pytest.raises(GeminiAuthenticationError):
        run(empty_state(), tmp_path, [article(1)], client=Unauthorized())


def test_expensive_legacy_pool_failure_is_retriable_and_never_exceeds_cap(tmp_path):
    state = empty_state()
    run(state, tmp_path, [article(1)])
    day = state['days'][NOW.date().isoformat()]
    template = next(iter(day['articles'].values()))
    for index in range(25):
        row = {**template, 'id': f'legacy-{index}', 'title_ko': f'독립적인 기존 보안 연구 테스트 {index}번'}
        day['articles'][row['id']] = row
    day.pop('news_curation', None)

    class FailingCuration(FakeGemini):
        def request(self, *args, **kwargs):
            raise GeminiError('테스트 실패')

    with pytest.raises(GeminiError, match='게시를 중단'):
        run(state, tmp_path, [], client=FailingCuration())
    assert day['articles'] == {} and day['hot'] == []
    assert len(day['news_candidates']) == 26
    assert day['news_curation']['status'] == 'unavailable'
