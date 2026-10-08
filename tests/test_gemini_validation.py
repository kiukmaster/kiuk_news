"""Semantic response checks retry within the existing physical request budget."""
import json
from unittest.mock import Mock

import pytest

from digest.common import load_config
from digest.gemini import (BudgetExceeded, Gemini, GeminiAuthenticationError,
                           GeminiHTTPError, GeminiIncompleteError,
                           GeminiResponseValidationError, response_text,
                           safe_response_diagnostic, SEMANTIC_RETRY_FEEDBACK)


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


def test_category_retry_adds_trusted_correction_before_unchanged_source_data(monkeypatch, capsys):
    client = make_client(monkeypatch)
    data = {'candidates': [{'id': 'paper-id', 'kind': 'paper', 'allowed_categories': ['tech'],
                            'title_original': 'Original 외부 제목'}]}
    client.session.post = Mock(side_effect=[response({'id': 'paper-id', 'category': 'ai'}),
                                             response({'id': 'paper-id', 'category': 'tech'})])

    def category_contract(result):
        if result['category'] != 'tech':
            raise GeminiResponseValidationError('기사 종류와 선정 카테고리가 일치하지 않습니다',
                                                 retry_code='CATEGORY')

    result = client.request('Original trusted instruction', data, {'type': 'object'}, client.summary_model,
                            attempts=2, validator=category_contract)
    first, second = [call.kwargs['json']['input'] for call in client.session.post.call_args_list]
    source_json = json.dumps(data, ensure_ascii=False)
    assert first == 'Original trusted instruction\n\nUNTRUSTED_DATA_JSON:\n' + source_json
    assert second == ('Original trusted instruction\n\nTRUSTED_VALIDATION_FEEDBACK:\n'
                      + SEMANTIC_RETRY_FEEDBACK['CATEGORY'] + '\n\nUNTRUSTED_DATA_JSON:\n' + source_json)
    assert second.index('TRUSTED_VALIDATION_FEEDBACK') < second.index('UNTRUSTED_DATA_JSON')
    assert result == {'id': 'paper-id', 'category': 'tech'}
    assert client.calls == client.session.post.call_count == 2 and client.tokens == 24
    output = capsys.readouterr().out
    assert output.strip() == '[Gemini 응답 보정 재시도] code=CATEGORY attempt=2/2'


@pytest.mark.parametrize('retry_code', [None, 'unknown', 'SYNTHETIC-secret-code', ['IDS'], {'code': 'IDS'}])
def test_unrecognized_retry_code_and_arbitrary_error_text_never_enter_prompt_or_log(
        monkeypatch, capsys, retry_code):
    client = make_client(monkeypatch)
    secret = 'private-model-output-and-provider-message'
    client.session.post = Mock(side_effect=[response({'id': secret}), response({'id': 'source-id'})])

    def check(result):
        if result['id'] != 'source-id':
            raise GeminiResponseValidationError(secret + '\n' + client.key, retry_code=retry_code)

    result = client.request('test', {'source': 'unchanged'}, {'type': 'object'}, client.summary_model,
                            attempts=2, validator=check)
    assert result == {'id': 'source-id'}
    inputs = [call.kwargs['json']['input'] for call in client.session.post.call_args_list]
    output = capsys.readouterr().out
    assert 'code=GENERIC' in output
    assert secret not in output and client.key not in output
    assert all(secret not in text and client.key not in text for text in inputs)
    assert 'SYNTHETIC-secret-code' not in output and all('SYNTHETIC-secret-code' not in text for text in inputs)
    assert SEMANTIC_RETRY_FEEDBACK['GENERIC'] in inputs[-1]


def test_semantic_feedback_is_replaced_instead_of_accumulated_across_attempts(monkeypatch):
    client = make_client(monkeypatch)
    client.session.post = Mock(side_effect=[response({'id': 'one'}), response({'id': 'two'}),
                                             response({'id': 'source-id'})])

    def check(result):
        if result['id'] == 'one':
            raise GeminiResponseValidationError('first failure', retry_code='SCORES')
        if result['id'] == 'two':
            raise GeminiResponseValidationError('second failure', retry_code='IDS')

    client.request('test', {'original': 'data'}, {'type': 'object'}, client.summary_model, validator=check)
    inputs = [call.kwargs['json']['input'] for call in client.session.post.call_args_list]
    assert 'TRUSTED_VALIDATION_FEEDBACK' not in inputs[0]
    assert SEMANTIC_RETRY_FEEDBACK['SCORES'] in inputs[1]
    assert SEMANTIC_RETRY_FEEDBACK['IDS'] in inputs[2]
    assert SEMANTIC_RETRY_FEEDBACK['SCORES'] not in inputs[2]
    assert inputs[2].count('TRUSTED_VALIDATION_FEEDBACK') == 1
    assert len({text.split('UNTRUSTED_DATA_JSON:\n', 1)[1] for text in inputs}) == 1
    assert client.calls == 3


def test_authentication_failure_after_semantic_feedback_stops_at_second_call(monkeypatch):
    client = make_client(monkeypatch)
    client.session.post = Mock(side_effect=[response({'id': 'invented'}), response({}, status=401)])
    with pytest.raises(GeminiAuthenticationError):
        client.request('test', {}, {'type': 'object'}, client.summary_model, validator=require_source_id)
    assert client.calls == client.session.post.call_count == 2


def test_retry_code_remains_safe_if_validator_changes_exception_attribute(monkeypatch, capsys):
    client = make_client(monkeypatch)
    client.session.post = Mock(side_effect=[response({'id': 'wrong'}), response({'id': 'source-id'})])

    def check(result):
        if result['id'] != 'source-id':
            error = GeminiResponseValidationError('private-message')
            error.retry_code = 'private-modified-retry-code'
            raise error

    client.request('test', {}, {'type': 'object'}, client.summary_model, validator=check)
    assert 'code=GENERIC' in capsys.readouterr().out
    assert 'private-modified-retry-code' not in client.session.post.call_args.kwargs['json']['input']


@pytest.mark.parametrize('invalid', [
    {'id': 'source-id', 'score': True, 'private': 'private-schema-value'},
    {'score': 3, 'private': 'private-schema-value'},
])
def test_completed_schema_failure_gets_safe_corrective_retry(monkeypatch, capsys, invalid):
    client = make_client(monkeypatch)
    schema = {'type': 'object', 'properties': {'id': {'type': 'string'}, 'score': {'type': 'integer'}},
              'required': ['id', 'score']}
    client.session.post = Mock(side_effect=[response(invalid), response({'id': 'source-id', 'score': 3})])
    result = client.request('test', {'original': 'data'}, schema, client.summary_model, attempts=2)
    assert result == {'id': 'source-id', 'score': 3} and client.calls == 2
    first, second = [call.kwargs['json']['input'] for call in client.session.post.call_args_list]
    assert 'TRUSTED_VALIDATION_FEEDBACK' not in first
    assert SEMANTIC_RETRY_FEEDBACK['GENERIC'] in second
    assert first.split('UNTRUSTED_DATA_JSON:\n', 1)[1] == second.split('UNTRUSTED_DATA_JSON:\n', 1)[1]
    output = capsys.readouterr().out
    assert 'code=GENERIC' in output
    assert 'private-schema-value' not in second and 'private-schema-value' not in output


def test_completed_malformed_model_json_gets_safe_corrective_retry(monkeypatch, capsys):
    client = make_client(monkeypatch)
    malformed = Mock(status_code=200)
    malformed.json.return_value = {'status': 'completed', 'steps': [
        {'type': 'model_output', 'content': [{'type': 'text', 'text': '{"private":"private-json-value",'}]}],
        'usage': {'total_tokens': 12}}
    client.session.post = Mock(side_effect=[malformed, response({'id': 'source-id'})])
    result = client.request('test', {}, {'type': 'object'}, client.summary_model, attempts=2)
    assert result == {'id': 'source-id'} and client.calls == 2 and client.tokens == 24
    second = client.session.post.call_args.kwargs['json']['input']
    output = capsys.readouterr().out
    assert SEMANTIC_RETRY_FEEDBACK['GENERIC'] in second and 'code=GENERIC' in output
    assert 'private-json-value' not in second and 'private-json-value' not in output


def test_repeated_schema_failure_exposes_fixed_error_without_private_values(monkeypatch, capsys):
    client = make_client(monkeypatch)
    client.session.post = Mock(return_value=response({'private': 'private-rejected-value'}))
    with pytest.raises(GeminiResponseValidationError, match='스키마 검증 실패') as error:
        client.request('test', {}, {'type': 'object', 'required': ['id']}, client.summary_model, attempts=2)
    assert client.calls == 2
    assert 'private-rejected-value' not in str(error.value)
    assert 'private-rejected-value' not in capsys.readouterr().out


def test_outer_response_json_failure_keeps_existing_connection_retry_path(monkeypatch, capsys):
    client = make_client(monkeypatch)
    malformed = Mock(status_code=200)
    malformed.json.side_effect = ValueError('private-http-response-value')
    client.session.post = Mock(side_effect=[malformed, response({'id': 'source-id'})])
    assert client.request('test', {}, {'type': 'object'}, client.summary_model, attempts=2) == {'id': 'source-id'}
    inputs = [call.kwargs['json']['input'] for call in client.session.post.call_args_list]
    assert inputs[0] == inputs[1] and 'TRUSTED_VALIDATION_FEEDBACK' not in inputs[1]
    assert 'private-http-response-value' not in capsys.readouterr().out


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
