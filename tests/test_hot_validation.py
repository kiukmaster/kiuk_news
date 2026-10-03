"""HOT semantic retries use mocked responses and never call an external API."""
import json
from unittest.mock import Mock

import pytest

from digest.common import load_config
from digest.gemini import (BudgetExceeded, Gemini, GeminiAuthenticationError,
                           GeminiResponseValidationError)


def make_client(monkeypatch, budget=80):
    monkeypatch.setenv('GEMINI_API_KEY', 'offline-test-placeholder')
    monkeypatch.delenv('GEMINI_MODEL', raising=False)
    monkeypatch.delenv('GEMINI_HOT_MODEL', raising=False)
    monkeypatch.setattr('digest.gemini.time.sleep', lambda _: None)
    cfg = load_config()
    cfg['max_api_calls_per_run'] = budget
    cfg['api_interval_seconds'] = 0
    return Gemini(cfg)


def response(result, status=200):
    payload = {'status': 'completed', 'steps': [
        {'type': 'model_output', 'content': [{'type': 'text', 'text': json.dumps(result)}]}],
        'usage': {'total_tokens': 12}}
    return Mock(status_code=status, json=Mock(return_value=payload))


def selection(ids, reason='검증된 근거를 바탕으로 선정했습니다.', shortfall=''):
    return {'picks': [{'id': ident, 'reason_ko': reason} for ident in ids],
            'shortfall_reason_ko': shortfall}


@pytest.mark.parametrize('invalid', [
    selection(['invented', 'two']),
    selection(['one', 'one']),
    selection(['one']),
    selection(['one', 'two'], reason=' '),
    selection(['one', 'two'], reason='가' * 201),
])
def test_hot_semantic_failure_recovers_with_validated_fallback(monkeypatch, invalid):
    client = make_client(monkeypatch)
    client.session.post = Mock(side_effect=[
        response(invalid), response(selection(['one', 'two']))])
    result = client.select_hot([{'id': 'one'}, {'id': 'two'}])
    assert [pick['id'] for pick in result['picks']] == ['one', 'two']
    assert result['model_used'] == client.summary_model
    assert client.calls == client.session.post.call_count == 2
    assert [call.kwargs['json']['model'] for call in client.session.post.call_args_list] == [
        client.hot_model, client.summary_model]


def test_hot_fallback_itself_retries_invalid_response(monkeypatch):
    client = make_client(monkeypatch)
    client.session.post = Mock(side_effect=[
        response(selection(['invented'])), response(selection(['invented'])),
        response(selection(['one']))])
    result = client.select_hot([{'id': 'one'}])
    assert result['picks'][0]['id'] == 'one'
    assert client.calls == client.session.post.call_count == 3


def test_hot_repeated_semantic_failure_stops_after_three_calls(monkeypatch):
    client = make_client(monkeypatch)
    client.session.post = Mock(return_value=response(selection(['invented'])))
    with pytest.raises(GeminiResponseValidationError, match='ID'):
        client.select_hot([{'id': 'one'}])
    assert client.calls == client.session.post.call_count == 3


def test_hot_semantic_retry_respects_remaining_physical_budget(monkeypatch):
    client = make_client(monkeypatch, budget=2)
    client.session.post = Mock(return_value=response(selection(['invented'])))
    with pytest.raises(BudgetExceeded):
        client.select_hot([{'id': 'one'}])
    assert client.calls == client.session.post.call_count == 2


@pytest.mark.parametrize('invalid_first', [False, True])
def test_hot_auth_failure_stops_without_further_calls(monkeypatch, invalid_first):
    client = make_client(monkeypatch)
    answers = [response(selection(['invented']))] if invalid_first else []
    answers.append(response({}, status=401))
    client.session.post = Mock(side_effect=answers)
    with pytest.raises(GeminiAuthenticationError):
        client.select_hot([{'id': 'one'}])
    assert client.calls == client.session.post.call_count == len(answers)


def test_hot_semantic_retry_when_both_configured_models_match(monkeypatch):
    client = make_client(monkeypatch)
    client.hot_model = client.summary_model
    client.session.post = Mock(side_effect=[
        response(selection(['invented'])), response(selection(['one']))])
    result = client.select_hot([{'id': 'one'}])
    assert result['picks'][0]['id'] == 'one'
    assert client.calls == client.session.post.call_count == 2
