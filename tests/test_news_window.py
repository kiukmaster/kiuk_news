"""Publication windows use Korean calendar days and preserve reusable evidence."""
from copy import deepcopy
from datetime import timedelta, timezone

from digest.pipeline import empty_state
from tests.test_core import FakeGemini, NOW, article, run


def test_yesterday_seen_publications_seed_today_and_reuse_completed_summaries(tmp_path):
    state = empty_state()
    yesterday = NOW - timedelta(days=1)
    older = article(1, yesterday - timedelta(days=1))
    recent = article(2, yesterday.replace(hour=23))
    run(state, tmp_path, [older, recent], yesterday.replace(hour=23))
    previous = state['days'][yesterday.date().isoformat()]
    assert recent['id'] in state['seen'] and older['id'] in previous['articles']
    previous_summary = previous['articles'][recent['id']]['summary_ko']

    client = run(state, tmp_path, [recent], NOW)
    today = state['days'][NOW.date().isoformat()]
    assert set(today['news_candidates']) == {recent['id']}
    assert set(today['articles']) == {recent['id']}
    assert today['articles'][recent['id']]['summary_ko'] == previous_summary
    assert today['news_candidates'][recent['id']]['title_original'] == recent['title_original']
    assert today['news_candidates'][recent['id']]['excerpt'] == recent['excerpt']
    assert today['news_candidates'][recent['id']]['first_seen_at'] == recent['first_seen_at']
    assert client.summarized == []
    assert all(pick['id'] == recent['id'] for pick in today['hot'])


def test_saved_pools_and_cached_cards_cannot_reintroduce_old_or_future_dates(tmp_path):
    state = empty_state()
    valid = article(1)
    run(state, tmp_path, [valid])
    day = state['days'][NOW.date().isoformat()]
    for index, published in [(2, NOW - timedelta(days=2)), (3, NOW + timedelta(minutes=1))]:
        row = article(index, published)
        row['first_seen_at'] = NOW.isoformat()
        cached = {**deepcopy(day['articles'][valid['id']]), **row}
        day['news_candidates'][row['id']] = cached
        day['articles'][row['id']] = cached
        day.setdefault('latest_articles', {})[row['id']] = cached
        state['pending'][row['id']] = row
        day['hot'].append({'id': row['id'], 'reason_ko': '만료된 검증 후보'})

    run(state, tmp_path, [], NOW)
    assert set(day['news_candidates']) == {valid['id']}
    assert set(day['articles']) == {valid['id']}
    assert not day['latest_articles'] and not state['pending']
    assert all(pick['id'] == valid['id'] for pick in day['hot'])


def test_utc_runner_time_uses_korean_report_day_and_window(tmp_path):
    local_now = NOW.replace(hour=0, minute=30)
    current = article(1, local_now)
    yesterday = article(2, local_now.replace(hour=0, minute=0) - timedelta(days=1))
    expired = article(3, local_now.replace(hour=0, minute=0) - timedelta(days=1, seconds=1))
    state = empty_state()
    run(state, tmp_path, [current, yesterday, expired], local_now.astimezone(timezone.utc))
    assert set(state['days']) == {local_now.date().isoformat()}
    day = state['days'][local_now.date().isoformat()]
    assert set(day['articles']) == {current['id'], yesterday['id']}


def test_body_metadata_outside_window_is_removed_from_retry_pool(tmp_path, monkeypatch):
    undated = article(1)
    undated['published_at'] = None
    monkeypatch.setattr('digest.pipeline.prepare_article', lambda fetcher, item, source, cfg:
                        {**item, 'published_at': (NOW - timedelta(days=2)).isoformat()})
    state = empty_state()
    client = run(state, tmp_path, [undated])
    day = state['days'][NOW.date().isoformat()]
    assert not day['articles'] and not day['news_candidates']
    assert not state['pending'] and client.summarized == []


def test_yesterday_curation_exclusion_remains_a_current_candidate(tmp_path):
    class ReverseCuration(FakeGemini):
        def request(self, instruction, data, schema, model, **kwargs):
            data = deepcopy(data)
            data['candidates'].reverse()
            return super().request(instruction, data, schema, model, **kwargs)

    state = empty_state()
    yesterday = NOW - timedelta(days=1)
    candidates = [article(index, yesterday) for index in range(25)]
    run(state, tmp_path, candidates, yesterday)
    excluded = candidates[-1]['id']
    assert state['seen'][excluded]['excluded_by_curation']
    client = run(state, tmp_path, [], NOW, client=ReverseCuration())
    day = state['days'][NOW.date().isoformat()]
    assert len(day['news_candidates']) == 25
    assert excluded in day['articles'] and excluded in client.summarized
