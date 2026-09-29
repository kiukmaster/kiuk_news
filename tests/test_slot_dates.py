from copy import deepcopy
from datetime import datetime, timedelta

from bs4 import BeautifulSoup

from digest.common import KST, load_config
from digest.pipeline import empty_state, new_day, repair_slot_dates, run_pipeline
from digest.render import render_site
from tests.test_core import FakeGemini, article


def test_legacy_overnight_slots_move_without_losing_articles():
    state = empty_state()
    for date in ('2026-09-28', '2026-09-29', '2026-09-30'):
        state['days'][date] = new_day(date)
    state['days']['2026-09-30']['articles']['kept'] = {'id': 'kept'}
    for date, at in (('2026-09-29', '2026-09-29T02:37:13+09:00'),
                     ('2026-09-30', '2026-09-30T00:44:37+09:00')):
        state['days'][date]['slots']['19:00'] = {'at': at, 'status': 'partial', 'new_count': 6}
    repair_slot_dates(state)
    assert not state['days']['2026-09-30']['slots']
    assert state['days']['2026-09-29']['slots']['19:00']['publish_at'] == '2026-09-29T19:00:00+09:00'
    assert state['days']['2026-09-28']['slots']['19:00']['at'].startswith('2026-09-29T02:37')
    assert state['days']['2026-09-30']['articles'] == {'kept': {'id': 'kept'}}
    repaired = deepcopy(state)
    repair_slot_dates(state)
    assert state == repaired


def test_legacy_external_dispatch_within_one_hour_keeps_its_date():
    state = empty_state()
    day = state['days']['2026-09-30'] = new_day('2026-09-30')
    day['slots']['06:00'] = {'at': '2026-09-30T05:02:00+09:00', 'status': 'ok'}
    repair_slot_dates(state)
    assert day['slots']['06:00']['publish_at'] == '2026-09-30T06:00:00+09:00'


def collect(tmp_path, now, schedule, target=None):
    state = empty_state()
    cfg = load_config()
    cfg.update(fetch_article_body=False, cve_enabled=False)
    row = article(601, now)
    run_pipeline(state, tmp_path / 'state', now, cfg, schedule, web=object(), gemini=FakeGemini(),
                 source_loader=lambda *a: ([row], []),
                 github_loader=lambda *a: ([], {'status': 'ok', 'name': 'Test', 'message': 'Test'}),
                 publication_at=target)
    return state


def test_late_collection_does_not_light_todays_future_evening(tmp_path):
    now = datetime(2026, 9, 30, 0, 44, tzinfo=KST)
    state = collect(tmp_path, now, '7 9 * * *')
    assert state['days']['2026-09-30']['slots'] == {}
    assert '19:00' in state['days']['2026-09-29']['slots']
    assert state['last_run']['day'] == '2026-09-30'
    assert state['last_run']['slot_day'] == '2026-09-29'
    output = tmp_path / 'site'
    render_site(state, output, now, load_config())
    page = BeautifulSoup((output / 'reports/2026-09-30.html').read_text(encoding='utf-8'), 'html.parser')
    assert page.select_one('.cycle-heading').get_text(' ', strip=True).endswith('0/3회 실행')
    assert not page.select('.cycle-chip.done, .cycle-chip.partial')
    assert page.select('#sec-ai [data-issue-card]')


def test_six_am_collection_lights_only_six_am(tmp_path):
    now = datetime(2026, 9, 30, 8, 15, tzinfo=KST)
    target = now.replace(hour=6, minute=0)
    state = collect(tmp_path, now, '7 20 * * *', target)
    assert set(state['days']['2026-09-30']['slots']) == {'06:00'}
    assert state['days']['2026-09-30']['slots']['06:00']['publish_at'] == target.isoformat()
    assert state['last_run']['new_count'] == 1


def test_old_run_retry_does_not_recreate_expired_report(tmp_path):
    now = datetime(2026, 9, 30, 8, 15, tzinfo=KST)
    target = now.replace(hour=6, minute=0) - timedelta(days=6)
    state = collect(tmp_path, now, '7 20 * * *', target)
    assert set(state['days']) == {'2026-09-30'}
