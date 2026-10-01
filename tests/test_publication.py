from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from digest.common import CRON_SLOTS, KST
from digest.publication import aware_date, main, publication_target, wait_seconds


@pytest.mark.parametrize('cron', CRON_SLOTS)
def test_on_time_schedule_waits_until_publication(cron):
    slot = CRON_SLOTS[cron]
    target = datetime(2026, 9, 29, int(slot[:2]), tzinfo=KST)
    created = target - timedelta(minutes=53)
    assert publication_target(created, 'schedule', cron) == target
    assert wait_seconds(target, created + timedelta(minutes=5)) == 48 * 60
    assert wait_seconds(target, target) == 0


@pytest.mark.parametrize(('created', 'deployed', 'expected'), [
    # Actual failed runs: the 19:00 schedule was created after KST midnight.
    ('2026-09-29T15:44:07Z', '2026-09-29T22:26:07Z', '2026-09-29T19:00:00+09:00'),
    ('2026-09-28T17:36:59Z', '2026-09-28T17:44:44Z', '2026-09-28T19:00:00+09:00'),
    # A run prepared on time may queue or be retried for multiple days.
    ('2026-09-29T09:07:00Z', '2026-10-01T22:26:07Z', '2026-09-29T19:00:00+09:00'),
])
def test_delayed_or_retried_run_keeps_original_publication_date(created, deployed, expected):
    target = publication_target(aware_date(created), 'schedule', '7 9 * * *')
    assert target == aware_date(expected)
    assert wait_seconds(target, aware_date(deployed)) == 0


def test_external_dispatch_keeps_request_date_after_queue_delay():
    created = aware_date('2026-09-29T18:07:00+09:00')
    target = publication_target(created, 'workflow_dispatch', slot='19:00')
    assert target == created.replace(hour=19, minute=0)
    assert wait_seconds(target, aware_date('2026-09-30T07:26:00+09:00')) == 0


@pytest.mark.parametrize(('created', 'cron', 'expected'), [
    ('2026-10-01T08:35:47+09:00', '7 5 * * *', '2026-10-01T06:00:00+09:00'),
    ('2026-09-30T18:31:05+09:00', '7 12 * * *', '2026-09-30T13:00:00+09:00'),
    ('2026-10-01T00:52:55+09:00', '7 18 * * *', '2026-09-30T19:00:00+09:00'),
])
def test_actual_late_events_with_explicit_kst_cron(created, cron, expected):
    assert publication_target(aware_date(created), 'schedule', cron) == aware_date(expected)


def test_explicit_external_target_preserves_date_even_if_event_created_next_day():
    target = aware_date('2026-09-30T19:00:00+09:00')
    assert publication_target(aware_date('2026-10-01T00:53:00+09:00'),
        'workflow_dispatch', slot='19:00', requested_at=target) == target


@pytest.mark.parametrize(('slot', 'target'), [
    ('manual', '2026-10-01T13:00:00+09:00'),
    ('13:00', '2026-10-01T19:00:00+09:00'),
    ('13:00', '2026-10-01T13:00:01+09:00'),
    ('13:00', '2026-10-01T13:00:00.001+09:00'),
])
def test_explicit_target_must_match_exact_slot(slot, target):
    with pytest.raises(ValueError):
        publication_target(aware_date('2026-10-01T12:07:00+09:00'),
            'workflow_dispatch', slot=slot, requested_at=aware_date(target))


def test_explicit_external_future_is_bounded():
    with pytest.raises(ValueError, match='Too early'):
        publication_target(aware_date('2026-10-01T17:00:00+09:00'),
            'workflow_dispatch', slot='19:00', requested_at=aware_date('2026-10-01T19:00:00+09:00'))


def test_plan_exports_external_frozen_timestamp(tmp_path, monkeypatch):
    output = tmp_path / 'step-output'
    monkeypatch.setenv('GITHUB_OUTPUT', str(output))
    monkeypatch.setenv('RUN_CREATED_AT', '2026-09-30T15:53:00Z')
    monkeypatch.setenv('GITHUB_EVENT_NAME', 'workflow_dispatch')
    monkeypatch.setenv('SCHEDULED_SLOT', '19:00')
    monkeypatch.setenv('REQUESTED_PUBLISH_AT', '2026-09-30T19:00:00+09:00')
    main(['plan'])
    assert output.read_text(encoding='utf-8') == 'publish_at=2026-09-30T19:00:00+09:00\n'


@pytest.mark.parametrize('slot', ('', 'manual'))
def test_manual_runs_publish_immediately(slot):
    assert publication_target(aware_date('2026-09-30T07:00:00+09:00'),
                              'workflow_dispatch', slot=slot) is None


def test_early_external_dispatch_still_rejected():
    with pytest.raises(ValueError, match='Too early'):
        publication_target(aware_date('2026-09-29T16:00:00+09:00'),
                           'workflow_dispatch', slot='19:00')


@pytest.mark.parametrize(('event', 'cron', 'slot'), [
    ('schedule', 'bad cron', 'manual'),
    ('workflow_dispatch', '', 'bad slot'),
])
def test_unknown_schedules_are_rejected(event, cron, slot):
    with pytest.raises(ValueError, match='Unknown'):
        publication_target(aware_date('2026-09-29T09:07:00Z'), event, cron, slot)


def test_plan_exports_timestamp_and_wait_uses_it(tmp_path, monkeypatch, capsys):
    output = tmp_path / 'step-output'
    output.write_text('existing=value\n', encoding='utf-8')
    monkeypatch.setenv('GITHUB_OUTPUT', str(output))
    monkeypatch.setenv('RUN_CREATED_AT', '2026-09-29T15:44:07Z')
    monkeypatch.setenv('GITHUB_EVENT_NAME', 'schedule')
    monkeypatch.setenv('SCHEDULE_CRON', '7 9 * * *')
    monkeypatch.setenv('SCHEDULED_SLOT', '')
    main(['plan'])
    assert output.read_text(encoding='utf-8') == (
        'existing=value\npublish_at=2026-09-29T19:00:00+09:00\n')
    monkeypatch.setenv('PUBLISH_AT', '2026-09-29T19:00:00+09:00')
    with patch('digest.publication.datetime') as clock, patch('digest.publication.time.sleep') as sleep:
        clock.fromisoformat.side_effect = datetime.fromisoformat
        clock.now.return_value = aware_date('2026-09-30T07:26:07+09:00')
        main(['wait'])
        sleep.assert_not_called()
    assert 'passed; deploying immediately' in capsys.readouterr().out


def test_wait_still_delays_early_publication(monkeypatch):
    monkeypatch.setenv('PUBLISH_AT', '2026-09-29T19:00:00+09:00')
    with patch('digest.publication.datetime') as clock, patch('digest.publication.time.sleep') as sleep:
        clock.fromisoformat.side_effect = datetime.fromisoformat
        clock.now.return_value = aware_date('2026-09-29T18:30:00+09:00')
        main(['wait'])
        sleep.assert_called_once_with(1800)


def test_ambiguous_timestamp_and_unbounded_wait_are_rejected():
    with pytest.raises(ValueError, match='timezone'):
        aware_date('2026-09-29T19:00:00')
    with pytest.raises(ValueError, match='one hour'):
        wait_seconds(aware_date('2026-09-29T19:00:00+09:00'),
                     aware_date('2026-09-29T16:00:00+09:00'))
