from datetime import datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.dispatch_news import (KST, DispatchError, UncertainDispatch, dispatch,
                                   publication_target, receipt_lock, run_gh)


def at(value):
    return datetime.fromisoformat(value)


@pytest.mark.parametrize(('clock', 'slot', 'expected'), [
    ('2026-10-01T05:07:00+09:00', '06:00', '2026-10-01T06:00:00+09:00'),
    ('2026-10-01T12:07:00+09:00', '13:00', '2026-10-01T13:00:00+09:00'),
    ('2026-10-01T18:07:00+09:00', '19:00', '2026-10-01T19:00:00+09:00'),
    ('2026-10-01T05:06:59+09:00', '06:00', '2026-09-30T06:00:00+09:00'),
    ('2026-10-01T17:00:00+09:00', '13:00', '2026-10-01T13:00:00+09:00'),
    ('2026-10-02T01:15:00+09:00', '19:00', '2026-10-01T19:00:00+09:00'),
    ('2026-10-01T03:07:00+00:00', '13:00', '2026-10-01T13:00:00+09:00'),
])
def test_latest_due_preparation_date_is_frozen(clock, slot, expected):
    assert publication_target(at(clock), slot).isoformat() == expected


@pytest.mark.parametrize(('target', 'slot', 'clock', 'message'), [
    ('2026-10-01T13:00:00', '13:00', '2026-10-01T12:07:00+09:00', 'timezone'),
    ('2026-10-01T19:00:00+09:00', '13:00', '2026-10-01T12:07:00+09:00', 'exact KST'),
    ('2026-10-01T13:00:01+09:00', '13:00', '2026-10-01T12:07:00+09:00', 'exact KST'),
    ('2026-10-01T13:00:00+09:00', '13:00', '2026-10-01T11:59:59+09:00', 'one hour'),
    ('2026-09-30T13:00:00+09:00', '13:00', '2026-10-01T13:00:00+09:00', 'one day'),
])
def test_override_rejects_ambiguous_wrong_or_unbounded_targets(target, slot, clock, message):
    with pytest.raises(ValueError, match=message):
        publication_target(at(clock), slot, target)


def test_override_accepts_utc_and_catchup_just_under_one_day():
    assert publication_target(at('2026-10-01T12:59:59+09:00'), '13:00',
                              '2026-09-30T04:00:00Z').isoformat() == '2026-09-30T13:00:00+09:00'


def test_clock_must_be_aware():
    with pytest.raises(ValueError, match='timezone'):
        publication_target(datetime(2026, 10, 1, 12, 7), '13:00')


def test_dry_run_has_no_requests_or_local_receipts(tmp_path):
    result = dispatch('13:00', now=at('2026-10-01T12:07:00+09:00'),
                      receipt_dir=tmp_path / 'absent', dry_run=True,
                      call_gh=lambda _: pytest.fail('No GitHub requests during dry-run'))
    assert result['action'] == 'dry-run'
    assert result['publish_at'] == '2026-10-01T13:00:00+09:00'
    assert 'publish_at=2026-10-01T13:00:00+09:00' in result['command']
    assert '--ref main' in result['command']
    assert not (tmp_path / 'absent').exists()


def gh_stub(calls, runs=(), mutation_error=None):
    def call(arguments):
        calls.append(arguments)
        if arguments[0] == 'api':
            return json.dumps({'workflow_runs': list(runs)})
        if mutation_error:
            raise mutation_error
        return ''
    return call


def matching_run(target='2026-10-01T13:00:00+09:00'):
    return {'id': 123, 'event': 'workflow_dispatch', 'head_branch': 'main',
            'display_title': f'News 13:00 {target}', 'html_url': 'https://github.com/o/r/actions/runs/123'}


def test_success_is_recorded_and_duplicate_calls_do_not_dispatch(tmp_path):
    calls = []
    result = dispatch('13:00', now=at('2026-10-01T12:07:00+09:00'),
                      receipt_dir=tmp_path, call_gh=gh_stub(calls))
    assert result['action'] == 'dispatched'
    assert len(calls) == 2 and calls[0][0] == 'api' and calls[1][0] == 'workflow'
    assert calls[1][-2:] == ['-f', 'publish_at=2026-10-01T13:00:00+09:00']
    assert json.loads(Path(result['receipt']).read_text())['status'] == 'accepted'
    result = dispatch('13:00', now=at('2026-10-01T17:00:00+09:00'), receipt_dir=tmp_path,
                      call_gh=lambda _: pytest.fail('Accepted receipt must deduplicate locally'))
    assert result['action'] == 'already-dispatched'
    assert not list(tmp_path.rglob('*.lock'))


def test_exact_remote_run_prevents_dispatch_from_second_host(tmp_path):
    calls = []
    result = dispatch('13:00', now=at('2026-10-01T12:07:00+09:00'), receipt_dir=tmp_path,
                      call_gh=gh_stub(calls, [matching_run()]))
    assert result['action'] == 'already-dispatched' and result['run_id'] == 123
    assert len(calls) == 1 and calls[0][0] == 'api'


def test_other_date_or_branch_does_not_falsely_deduplicate(tmp_path):
    wrong_branch = {**matching_run(), 'head_branch': 'other'}
    wrong_date = matching_run('2026-09-30T13:00:00+09:00')
    calls = []
    result = dispatch('13:00', now=at('2026-10-01T12:07:00+09:00'), receipt_dir=tmp_path,
                      call_gh=gh_stub(calls, [wrong_branch, wrong_date]))
    assert result['action'] == 'dispatched'
    assert len(calls) == 2


def test_uncertain_dispatch_is_reconciled_without_mutating_retry(tmp_path):
    calls = []
    with pytest.raises(DispatchError, match='timeout'):
        dispatch('13:00', now=at('2026-10-01T12:07:00+09:00'), receipt_dir=tmp_path,
                 call_gh=gh_stub(calls, mutation_error=DispatchError('timeout')))
    receipt = next(tmp_path.rglob('*.json'))
    assert json.loads(receipt.read_text())['status'] == 'uncertain'
    retry_calls = []
    result = dispatch('13:00', now=at('2026-10-01T12:08:00+09:00'), receipt_dir=tmp_path,
                      call_gh=gh_stub(retry_calls, [matching_run()]))
    assert result['action'] == 'already-dispatched'
    assert len(retry_calls) == 1 and retry_calls[0][0] == 'api'
    assert json.loads(receipt.read_text())['status'] == 'accepted'


def test_unresolved_uncertainty_never_blindly_retries(tmp_path):
    with pytest.raises(DispatchError):
        dispatch('13:00', now=at('2026-10-01T12:07:00+09:00'), receipt_dir=tmp_path,
                 call_gh=gh_stub([], mutation_error=DispatchError('timeout')))
    calls = []
    with pytest.raises(UncertainDispatch, match='No duplicate request was sent'):
        dispatch('13:00', now=at('2026-10-01T12:08:00+09:00'), receipt_dir=tmp_path,
                 call_gh=gh_stub(calls))
    assert len(calls) == 1 and calls[0][0] == 'api'


def test_atomic_slot_lock_blocks_second_worker_without_requests(tmp_path):
    directory = tmp_path / 'kiukmaster--kiuk_news' / 'update-news.yml'
    with receipt_lock(directory, '2026-10-01-1300'):
        with pytest.raises(DispatchError, match='slot lock'):
            dispatch('13:00', now=at('2026-10-01T12:07:00+09:00'), receipt_dir=tmp_path,
                     call_gh=lambda _: pytest.fail('No request from overlapping task'))
    assert not list(directory.glob('*.lock'))


def test_preflight_failure_creates_no_uncertain_request_receipt(tmp_path):
    with pytest.raises(DispatchError, match='read failed'):
        dispatch('13:00', now=at('2026-10-01T12:07:00+09:00'), receipt_dir=tmp_path,
                 call_gh=lambda _: (_ for _ in ()).throw(DispatchError('read failed')))
    assert not list(tmp_path.rglob('*.json'))
    assert not list(tmp_path.rglob('*.lock'))


def test_gh_subprocess_diagnostics_do_not_expose_stderr(monkeypatch):
    from scripts import dispatch_news
    def run(*args, **kwargs):
        assert args[0][:2] == ['gh', 'api']
        assert kwargs['capture_output'] is True
        assert kwargs['timeout'] == 60
        return SimpleNamespace(returncode=1, stdout='', stderr='private-token-must-not-leak')
    monkeypatch.setattr(dispatch_news.subprocess, 'run', run)
    with pytest.raises(DispatchError) as error:
        run_gh(['api', 'repos/o/r'])
    assert 'private-token' not in str(error.value)


def test_invalid_repository_rejected_even_in_dry_run(tmp_path):
    with pytest.raises(ValueError, match='owner/name'):
        dispatch('13:00', now=at('2026-10-01T12:07:00+09:00'), repository='bad/repo/extra',
                 receipt_dir=tmp_path, dry_run=True)
