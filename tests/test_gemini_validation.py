"""Semantic response checks retry within the existing physical request budget."""
import json
from unittest.mock import Mock

import pytest

from digest.common import load_config
from digest.gemini import (BudgetExceeded, Gemini, GeminiAuthenticationError,
                           GeminiHTTPError, GeminiIncompleteError,
                           GeminiResponseValidationError, response_text,
                           safe_response_diagnostic)


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


def unfinished(status='incomplete', reason='MAX_TOKENS', tokens=12):
    # Valid-looking partial JSON must never pass to json.loads/the validator.
    payload = {'status': status, 'errors': [{'code': reason, 'message': 'provider diagnostic'}],
               'steps': [{'type': 'model_output', 'content': [
                   {'type': 'text', 'text': '{"id":"partial-source-id"}'}]}],
               'usage': {'total_tokens': tokens, 'total_output_tokens': 8192,
                         'total_thought_tokens': 4000}}
    return Mock(status_code=200, json=Mock(return_value=payload))


def test_token_limit_retry_never_parses_partial_json_and_expands_output_cap(monkeypatch):
    client = make_client(monkeypatch)
    validator = Mock(side_effect=require_source_id)
    replies = iter([unfinished(), response({'id': 'source-id'})])
    limits = []

    def post(*args, **kwargs):
        limits.append(kwargs['json']['generation_config']['max_output_tokens'])
        return next(replies)

    client.session.post = Mock(side_effect=post)
    result = client.request('test', {}, {'type': 'object'}, 'gemini-3.8-flash',
                            attempts=2, validator=validator)
    assert result == {'id': 'source-id'}
    assert limits == [8192, 16384]
    assert client.calls == client.session.post.call_count == 2
    assert client.tokens == 24
    validator.assert_called_once_with({'id': 'source-id'})


def test_incomplete_retries_only_within_reserved_physical_budget(monkeypatch):
    client = make_client(monkeypatch, budget=1)
    client.session.post = Mock(return_value=unfinished())
    with pytest.raises(BudgetExceeded):
        client.request('test', {}, {'type': 'object'}, 'gemini-3.8-flash', attempts=2)
    assert client.calls == client.session.post.call_count == 1
    assert client.tokens == 12


def test_repeated_token_exhaustion_stops_at_attempt_bound_and_model_cap(monkeypatch):
    client = make_client(monkeypatch)
    client.session.post = Mock(return_value=unfinished())
    with pytest.raises(GeminiIncompleteError) as error:
        client.request('test', {}, {'type': 'object'}, 'gemini-3.8-flash',
                       attempts=2, max_output_tokens=65536)
    assert error.value.status == 'incomplete'
    assert error.value.reason == 'MAX_TOKENS' and error.value.retryable
    assert error.value.diagnostic['output_limit'] == 65536
    assert client.calls == 2
    assert client.session.post.call_args.kwargs['json']['generation_config']['max_output_tokens'] == 65536


@pytest.mark.parametrize('status,code', [
    ('incomplete', 'safety'), ('failed', 'content_blocked'), ('failed', 'recitation'),
    ('failed', 'authentication'), ('failed', 'permission_denied'),
    ('failed', 'quota_exceeded'), ('failed', 'payment_required'),
    ('incomplete', 'unknown-provider-code'), ('queued', 'service_unavailable'),
    ('in_progress', 'service_unavailable'),
])
def test_nonretryable_failures_stop_without_parsing_output(monkeypatch, status, code):
    client = make_client(monkeypatch)
    validator = Mock()
    client.session.post = Mock(return_value=unfinished(status, code))
    with pytest.raises(GeminiIncompleteError) as error:
        client.request('test', {}, {'type': 'object'}, 'gemini-3.8-flash',
                       attempts=2, validator=validator)
    assert not error.value.retryable
    assert client.calls == 1
    validator.assert_not_called()


@pytest.mark.parametrize('code', ['api_error', 'service_unavailable', 'rate_limit_exceeded', 'too_many_requests'])
def test_declared_transient_failed_state_recovers_with_bounded_retry(monkeypatch, code):
    client = make_client(monkeypatch)
    client.session.post = Mock(side_effect=[unfinished('failed', code), response({'id': 'source-id'})])
    result = client.request('test', {}, {'type': 'object'}, client.summary_model, attempts=2)
    assert result == {'id': 'source-id'} and client.calls == 2


def test_failed_rate_limit_cooldown_is_shared_with_next_request(monkeypatch):
    client = make_client(monkeypatch)
    now = [100.0]
    delays = []
    monkeypatch.setattr('digest.gemini.time.monotonic', lambda: now[0])
    monkeypatch.setattr('digest.gemini.random.uniform', lambda *_: 0)

    def sleep(seconds):
        delays.append(seconds)
        now[0] += seconds

    monkeypatch.setattr('digest.gemini.time.sleep', sleep)
    replies = iter([unfinished('failed', 'rate_limit_exceeded'), response({'id': 'source-id'})])
    call_times = []

    def post(*args, **kwargs):
        call_times.append(now[0])
        return next(replies)

    client.session.post = Mock(side_effect=post)
    with pytest.raises(GeminiIncompleteError):
        client.request('test', {}, {'type': 'object'}, client.summary_model, attempts=1)
    assert client.request('test', {}, {'type': 'object'}, client.summary_model) == {'id': 'source-id'}
    assert call_times == [100.0, 130.0] and delays == [30.0]
    assert client.calls == 2


def test_response_metadata_never_logs_provider_prose_partial_text_or_continuation_token():
    secret = 'synthetic-private-value-keep-out-of-logs'
    payload = {'status': 'incomplete', 'continuation_token': secret,
               'errors': [{'code': 'https://private.example/' + secret, 'message': secret}],
               'finish_reason': secret,
               'steps': [{'type': 'model_output', 'content': [{'type': 'text', 'text': secret}]}],
               'usage': {'total_output_tokens': 50, 'total_thought_tokens': 60}}
    with pytest.raises(GeminiIncompleteError) as error:
        response_text(payload, 8192)
    diagnostic = error.value.diagnostic
    assert secret not in str(error.value) and secret not in json.dumps(diagnostic)
    assert diagnostic['reason'] == 'UNKNOWN' and not diagnostic['retryable']
    assert diagnostic['continuation_available'] is True
    assert diagnostic['output_tokens'] == 50 and diagnostic['thought_tokens'] == 60


def test_safety_metadata_takes_precedence_over_token_limit():
    diagnostic = safe_response_diagnostic({'status': 'incomplete', 'finish_reason': 'MAX_TOKENS',
        'errors': [{'code': 'safety'}], 'usage': {'total_output_tokens': 8192}}, 8192)
    assert diagnostic['reason'] == 'SAFETY' and not diagnostic['retryable']


def test_incomplete_status_alone_does_not_imply_token_exhaustion():
    diagnostic = safe_response_diagnostic({'status': 'incomplete', 'continuation_token': 'opaque'}, 8192)
    assert diagnostic['reason'] == 'UNKNOWN' and not diagnostic['retryable']


def test_exact_reported_output_cap_can_identify_token_exhaustion_without_reason():
    diagnostic = safe_response_diagnostic({'status': 'incomplete',
        'usage': {'total_output_tokens': 8192, 'total_thought_tokens': 0}}, 8192)
    assert diagnostic['reason'] == 'MAX_TOKENS' and diagnostic['retryable']


def test_combined_thought_and_output_cap_identifies_reasoning_token_exhaustion():
    diagnostic = safe_response_diagnostic({'status': 'incomplete',
        'usage': {'total_output_tokens': 3000, 'total_thought_tokens': 13384}}, 16384)
    assert diagnostic['reason'] == 'MAX_TOKENS' and diagnostic['retryable']
    assert diagnostic['output_tokens'] == 3000 and diagnostic['thought_tokens'] == 13384


def test_below_combined_cap_without_diagnostic_remains_unknown():
    diagnostic = safe_response_diagnostic({'status': 'incomplete',
        'usage': {'total_output_tokens': 3000, 'total_thought_tokens': 13000}}, 16384)
    assert diagnostic['reason'] == 'UNKNOWN' and not diagnostic['retryable']


def test_documented_error_uri_is_classified_without_exposing_uri():
    diagnostic = safe_response_diagnostic({'status': 'failed', 'errors': [
        {'code': 'https://ai.google.dev/errors/service_unavailable', 'message': 'do not expose'}]})
    assert diagnostic['reason'] == 'SERVICE_UNAVAILABLE' and diagnostic['retryable']
    assert diagnostic['error_codes'] == ['SERVICE_UNAVAILABLE']
    assert 'https://' not in json.dumps(diagnostic)
