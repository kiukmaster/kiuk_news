"""Offline contract/integration tests. No requests are sent to Google or news sites."""
import importlib.util
import json
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest

from digest.common import load_config
from digest.gemini import Gemini, GeminiError, GeminiHTTPError, GeminiAuthenticationError, BudgetExceeded, ENDPOINT, HOT_CHUNK_SIZE
from digest.pipeline import hot_round_calls


def make_client(monkeypatch):
    monkeypatch.setenv('GEMINI_API_KEY', 'unit-test-placeholder-not-a-real-key')
    monkeypatch.delenv('GEMINI_MODEL', raising=False)
    monkeypatch.delenv('GEMINI_HOT_MODEL', raising=False)
    monkeypatch.setattr('digest.gemini.time.sleep', lambda _: None)
    cfg = load_config()
    cfg['api_interval_seconds'] = 0
    return Gemini(cfg)


def api_response(result, status_code=200):
    response = Mock(status_code=status_code)
    response.json.return_value = {'status': 'completed', 'steps': [
        {'type': 'model_output', 'content': [{'type': 'text', 'text': json.dumps(result)}]}],
        'usage': {'total_tokens': 42}}
    return response


def test_interactions_contract(monkeypatch):
    client = make_client(monkeypatch)
    client.session.post = Mock(return_value=api_response({'ok': True}))
    schema = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok']}
    assert client.request('test', {}, schema, client.summary_model) == {'ok': True}
    args, kwargs = client.session.post.call_args
    assert args == (ENDPOINT,)
    assert kwargs['json']['store'] is False
    assert kwargs['json']['stream'] is False
    assert kwargs['json']['response_format']['schema'] == schema
    assert kwargs['headers']['x-goog-api-key'] == client.key
    assert client.key not in json.dumps(kwargs['json'])
    assert client.tokens == 42


def test_429_retries_then_success(monkeypatch):
    client = make_client(monkeypatch)
    client.session.post = Mock(side_effect=[api_response({}, 429), api_response({'ok': True})])
    assert client.request('test', {}, {'type': 'object'}, client.summary_model)['ok']
    assert client.calls == 2


def test_fatal_error_does_not_leak_secret(monkeypatch):
    client = make_client(monkeypatch)
    client.session.post = Mock(return_value=api_response({}, 403))
    with pytest.raises(GeminiError) as exc:
        client.request('test', {}, {'type': 'object'}, client.summary_model)
    assert client.key not in str(exc.value)
    assert client.calls == 1


def test_401_identifies_auth_key_without_leaking_secret(monkeypatch):
    client = make_client(monkeypatch)
    client.session.post = Mock(return_value=api_response({}, 401))
    with pytest.raises(GeminiAuthenticationError, match='GEMINI_API_KEY') as exc:
        client.request('test', {}, {'type': 'object'}, client.summary_model)
    assert client.key not in str(exc.value)
    assert client.calls == 1


def test_schema_failure_retries(monkeypatch):
    client = make_client(monkeypatch)
    client.session.post = Mock(return_value=api_response({'ok': 'not a boolean'}))
    with pytest.raises(GeminiError):
        client.request('test', {}, {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}}, client.summary_model)
    assert client.calls == 3


def test_budget_bound(monkeypatch):
    client = make_client(monkeypatch)
    client.calls = client.cfg['max_api_calls_per_run']
    client.session.post = Mock()
    with pytest.raises(BudgetExceeded):
        client.request('test', {}, {}, client.summary_model)
    client.session.post.assert_not_called()


def test_bad_summary_ids(monkeypatch):
    client = make_client(monkeypatch)
    client.request = Mock(return_value={'articles': [{'id': 'invented-id'}]})
    with pytest.raises(GeminiError, match='ID'):
        client.summarize([{'id': 'real-id'}])


def test_english_summary_is_rejected(monkeypatch):
    client = make_client(monkeypatch)
    client.request = Mock(return_value={'articles': [{'id': '1', 'relevant': True,
        'title_ko': 'Title', 'summary_ko': 'Not a Korean summary.'}]})
    with pytest.raises(GeminiError, match='한국어'):
        client.summarize([{'id': '1'}])


def test_hot_unsupported_id(monkeypatch):
    client = make_client(monkeypatch)
    client.request = Mock(return_value={'picks': [{'id': 'invented', 'reason_ko': '테스트'}], 'shortfall_reason_ko': ''})
    with pytest.raises(GeminiError, match='ID'):
        client.select_hot([{'id': 'real'}])


def test_hot_shortfall_requires_reason(monkeypatch):
    client = make_client(monkeypatch)
    client.request = Mock(return_value={'picks': [{'id': '1', 'reason_ko': '테스트'}], 'shortfall_reason_ko': ''})
    with pytest.raises(GeminiError, match='개수'):
        client.select_hot([{'id': '1'}, {'id': '2'}])


def test_hot_ranks_every_candidate_in_bounded_gemini_rounds(monkeypatch):
    client = make_client(monkeypatch)
    articles = [{'id': str(index), 'title_ko': f'테스트 {index}', 'summary_ko': '테스트 요약'} for index in range(170)]
    calls = []

    def choose(_instruction, data, _schema, model, attempts=3):
        calls.append((model, attempts, [row['id'] for row in data['candidates']]))
        return {'picks': [{'id': row['id'], 'reason_ko': '테스트 이유'}
                          for row in data['candidates'][:10]], 'shortfall_reason_ko': ''}

    client.request = choose
    result = client.select_hot(articles)
    assert len(result['picks']) == 10
    assert result['model_used'] == client.hot_model
    assert all(len(ids) <= HOT_CHUNK_SIZE for _, _, ids in calls)
    assert set().union(*(set(ids) for _, _, ids in calls[:-1])) == {row['id'] for row in articles}
    assert [model for model, _, _ in calls] == [client.summary_model] * 3 + [client.hot_model]
    assert len(calls[-1][2]) == 30
    assert hot_round_calls(80) == 1
    assert hot_round_calls(170) == 4
    assert hot_round_calls(530) == 8


@pytest.mark.parametrize('status', [429, 503])
def test_hot_transient_error_falls_back_to_other_gemini_model(monkeypatch, status):
    client = make_client(monkeypatch)
    calls = []

    def choose(_instruction, data, _schema, model, attempts=3):
        calls.append((model, attempts))
        if model == client.hot_model:
            raise GeminiHTTPError(status)
        return {'picks': [{'id': row['id'], 'reason_ko': '테스트 이유'}
                          for row in data['candidates']], 'shortfall_reason_ko': ''}

    client.request = choose
    result = client.select_hot([{'id': 'real'}])
    assert result['picks'][0]['id'] == 'real'
    assert result['model_used'] == client.summary_model
    assert calls == [(client.hot_model, 1), (client.summary_model, 2)]


def test_hot_auth_error_does_not_retry_another_model(monkeypatch):
    client = make_client(monkeypatch)
    client.request = Mock(side_effect=GeminiHTTPError(401))
    with pytest.raises(GeminiHTTPError, match='GEMINI_API_KEY'):
        client.select_hot([{'id': 'real'}])
    assert client.request.call_count == 1


def test_state_branch_across_two_runners(tmp_path, monkeypatch):
    """Local bare Git remote simulates distinct Actions runners, without any account."""
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('state_branch', root / 'scripts/state_branch.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    remote, source, runner2 = (tmp_path / x for x in ('remote.git', 'source', 'runner2'))
    def git(*args, cwd=None):
        return subprocess.run(['git', *args], cwd=cwd, check=True, text=True, capture_output=True).stdout.strip()
    git('init', '--bare', str(remote))
    git('init', '-b', 'main', str(source))
    git('config', 'user.name', 'Test', cwd=source)
    git('config', 'user.email', 'test@example.invalid', cwd=source)
    (source / 'README.md').write_text('source code, not news state', encoding='utf-8')
    git('add', '.', cwd=source)
    git('commit', '-m', 'Initial source', cwd=source)
    git('remote', 'add', 'origin', str(remote), cwd=source)
    git('push', '-u', 'origin', 'main', cwd=source)
    monkeypatch.chdir(source)
    module.restore(source / 'state')
    (source / 'state/state.json').write_text('{"runs":1}', encoding='utf-8')
    module.save(source / 'state')
    assert git('--git-dir', str(remote), 'ls-tree', '--name-only', 'news-state') == 'state.json'
    assert git('--git-dir', str(remote), 'ls-tree', '--name-only', 'main') == 'README.md'
    git('clone', '-b', 'main', str(remote), str(runner2))
    monkeypatch.chdir(runner2)
    module.restore(runner2 / 'state')
    assert json.loads((runner2 / 'state/state.json').read_text()) == {'runs': 1}
    (runner2 / 'state/state.json').write_text('{"runs":2}', encoding='utf-8')
    module.save(runner2 / 'state')
    assert json.loads(git('--git-dir', str(remote), 'show', 'news-state:state.json')) == {'runs': 2}
    assert (runner2 / 'README.md').read_text() == 'source code, not news state'
