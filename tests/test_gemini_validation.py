"""Semantic response checks retry within the existing physical request budget."""
import json
from unittest.mock import Mock

import pytest

from digest.common import load_config
from digest.gemini import (BudgetExceeded, Gemini, GeminiAuthenticationError,
                           GeminiHTTPError, GeminiResponseValidationError)


def make_client(monkeypatch, budget=80):
    monkeypatch.setenv('GEMINI_API_KEY', 'offline-test-placeholder')
    monkeypatch.setattr('digest.gemini.time.sleep', lambda _: None)
    cfg = load_config()
    cfg['max_api_calls_per_run'] = budget
    cfg['api_interval_seconds'] = 0
    return Gemini(cfg)


def response(result, status=200):
    result = {'status': 'completed', 'steps': [
        {'type': 'model_output', 'content': [{'type': 'text', 'text': json.dumps(result)}]}],
        'usage': {'total_tokens': 12}}
    return Mock(status_code=status, json=Mock(return_value=result))


def require_source_id(result):
    if result.get('id') != 'source-id':
        raise GeminiResponseValidationError('응답의 ID가 원문과 다릅니다')


def test_semantic_failure_retries_before_return(monkeypatch):
    client = make_client(monkeypatch)
    client.session.post = Mock(side_effect=[
        response({'id': 'invented-id'}), response({'id': 'source-id'})])
    validate = Mock(side_effect=require_source_id)
    result = client.request('test', {}, {'type': 'object'}, client.summary_model,
                            validator=validate)
    assert result == {'id': 'source-id'}
    assert client.calls == client.session.post.call_count == validate.call_count == 2
    assert client.tokens == 24


def test_semantic_failure_exhausts_only_three_attempts(monkeypatch):
    client = make_client(monkeypatch)
    client.session.post = Mock(return_value=response({'id': 'invented-id'}))
    with pytest.raises(GeminiResponseValidationError, match='원문'):
        client.request('test', {}, {'type': 'object'}, client.summary_model,
                       validator=require_source_id)
    assert client.calls == client.session.post.call_count == 3


def test_semantic_retry_respects_physical_budget(monkeypatch):
    client = make_client(monkeypatch, budget=2)
    client.session.post = Mock(return_value=response({'id': 'invented-id'}))
    with pytest.raises(BudgetExceeded):
        client.request('test', {}, {'type': 'object'}, client.summary_model,
                       validator=require_source_id)
    assert client.calls == client.session.post.call_count == 2


@pytest.mark.parametrize('status', [401, 403])
def test_http_failure_is_not_semantic_retry(monkeypatch, status):
    client = make_client(monkeypatch)
    client.session.post = Mock(return_value=response({}, status=status))
    validate = Mock(side_effect=require_source_id)
    expected = GeminiAuthenticationError if status == 401 else GeminiHTTPError
    with pytest.raises(expected):
        client.request('test', {}, {'type': 'object'}, client.summary_model,
                       validator=validate)
    assert client.calls == client.session.post.call_count == 1
    validate.assert_not_called()


def test_semantic_retry_keeps_existing_interval(monkeypatch):
    client = make_client(monkeypatch)
    client.cfg['api_interval_seconds'] = 10
    now, sleeps = [100.0], []
    monkeypatch.setattr('digest.gemini.time.monotonic', lambda: now[0])

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr('digest.gemini.time.sleep', sleep)
    call_times = []
    answers = iter([response({'id': 'invented-id'}), response({'id': 'source-id'})])

    def post(*args, **kwargs):
        call_times.append(now[0])
        return next(answers)

    client.session.post = Mock(side_effect=post)
    client.request('test', {}, {'type': 'object'}, client.summary_model,
                   validator=require_source_id)
    assert call_times == [100.0, 110.0]
    assert sum(sleeps) == 10
