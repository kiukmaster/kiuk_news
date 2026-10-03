"""Daily selection is bounded, atomic and entirely offline in these tests."""
import copy
import json
from collections import Counter
from unittest.mock import Mock

import pytest

from digest.common import load_config
from digest.curation import (CURATION_CHUNK_SIZE, SCORE_KEYS, curate_candidates,
                             curation_max_calls)
from digest.gemini import (BudgetExceeded, Gemini, GeminiAuthenticationError,
                           GeminiError, GeminiResponseValidationError)


def make_client(monkeypatch, budget=80):
    monkeypatch.setenv('GEMINI_API_KEY', 'offline-test-placeholder')
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


def pick(ident, category=None, related=()):
    result = {'id': ident, 'scores': dict.fromkeys(SCORE_KEYS, 3),
              'reason_ko': '제공된 근거의 파급력과 시의성을 고려했습니다.', 'related_ids': list(related)}
    if category is not None:
        result['category'] = category
    return result


def selection(*picks, shortfall=''):
    return {'picks': list(picks), 'shortfall_reason_ko': shortfall}


def payload_candidates(call):
    return json.loads(call['json']['input'].split('UNTRUSTED_DATA_JSON:\n', 1)[1])['candidates']


@pytest.mark.parametrize('kind,counts', [
    ('news', {0: 0, 1: 2, 300: 2, 301: 6, 600: 6, 601: 8, 1801: 22}),
    ('cve', {0: 0, 1: 2, 300: 2, 301: 6, 600: 6, 601: 8, 4501: 38}),
])
def test_call_bound_counts_all_rounds_and_both_physical_attempts(kind, counts):
    for count, expected in counts.items():
        assert curation_max_calls(count, kind) == expected


def test_every_candidate_is_screened_and_final_news_cap_is_per_category(monkeypatch):
    client = make_client(monkeypatch)
    categories = ('ai', 'security', 'tech', 'event', 'github')
    candidates = []
    for number in range(1801):
        category = categories[number % 5]
        candidates.append({'id': str(number), 'category_hint': category,
                           'kind': category if category in ('event', 'github') else 'news'})
    seen = set()

    def choose(*args, **kwargs):
        rows = payload_candidates(kwargs)
        assert len(rows) <= CURATION_CHUNK_SIZE
        seen.update(row['id'] for row in rows)
        counts, picks = Counter(), []
        for row in rows:
            category = row['category_hint']
            if counts[category] < 20:
                picks.append(pick(row['id'], category))
                counts[category] += 1
        return response(selection(*picks))

    client.session.post = Mock(side_effect=choose)
    original = copy.deepcopy(candidates)
    result = curate_candidates(client, candidates)
    assert seen == {row['id'] for row in candidates}
    assert Counter(row['category'] for row in result['picks']) == dict.fromkeys(categories, 20)
    assert client.calls <= curation_max_calls(len(candidates), 'news')
    assert all(call.kwargs['json']['model'] == 'gemini-3.8-flash'
               for call in client.session.post.call_args_list)
    assert all(call.kwargs['json']['generation_config']['max_output_tokens'] == 16384
               for call in client.session.post.call_args_list)
    assert candidates == original


def test_duplicate_members_are_preserved_when_representative_changes_between_rounds(monkeypatch):
    client = make_client(monkeypatch)
    candidates = [{'id': f'CVE-2026-{10000 + number}'} for number in range(301)]
    first, duplicate, final = candidates[0]['id'], candidates[1]['id'], candidates[-1]['id']
    client.session.post = Mock(side_effect=[
        response(selection(pick(first, related=[duplicate]))),
        response(selection(pick(final))),
        response(selection(pick(final, related=[first]))),
    ])
    result = curate_candidates(client, candidates, kind='cve')
    assert result['picks'][0]['id'] == final
    assert result['picks'][0]['related_ids'] == [first, duplicate]
    final_data = payload_candidates(client.session.post.call_args_list[-1].kwargs)
    assert final_data[0]['already_grouped_ids'] == [duplicate]
    assert client.calls == 3


def test_cap_violation_is_retried_before_selection_is_returned(monkeypatch):
    client = make_client(monkeypatch)
    candidates = [{'id': str(number)} for number in range(21)]
    client.session.post = Mock(side_effect=[
        response(selection(*(pick(row['id'], 'ai') for row in candidates))),
        response(selection(*(pick(row['id'], 'ai') for row in candidates[:20]))),
    ])
    result = curate_candidates(client, candidates)
    assert len(result['picks']) == 20
    assert client.calls == 2


@pytest.mark.parametrize('malformed', [
    selection(pick('outside', 'ai')),
    selection(pick('one', 'ai'), pick('one', 'ai')),
    selection(pick('one', 'ai', related=['outside'])),
    selection(pick('one', 'ai', related=['one'])),
    selection(pick('one', 'ai', related=['two']), pick('two', 'tech')),
    selection(pick('one', 'ai', related=['three']), pick('two', 'tech', related=['three'])),
    selection(shortfall=''),
    selection(dict(pick('one', 'ai'), reason_ko='Unsupported English reason')),
    selection(dict(pick('one', 'ai'), scores=dict.fromkeys(SCORE_KEYS, True))),
])
def test_invalid_selection_is_rejected_atomically_after_only_two_attempts(monkeypatch, malformed):
    client = make_client(monkeypatch)
    client.session.post = Mock(return_value=response(malformed))
    candidates = [{'id': ident} for ident in ('one', 'two', 'three')]
    original = copy.deepcopy(candidates)
    with pytest.raises(GeminiError):
        curate_candidates(client, candidates)
    assert client.calls == client.session.post.call_count == 2
    assert candidates == original


@pytest.mark.parametrize('kind,category', [
    ('paper', 'ai'), ('event', 'tech'), ('github', 'security'), ('news', 'github'),
])
def test_source_kind_restricts_category(monkeypatch, kind, category):
    client = make_client(monkeypatch)
    client.session.post = Mock(return_value=response(selection(pick('one', category))))
    with pytest.raises(GeminiResponseValidationError, match='카테고리'):
        curate_candidates(client, [{'id': 'one', 'kind': kind}])
    assert client.calls == 2


def test_not_enough_budget_prevents_even_first_screening_call(monkeypatch):
    client = make_client(monkeypatch, budget=5)
    client.session.post = Mock()
    with pytest.raises(BudgetExceeded, match='6회'):
        curate_candidates(client, [{'id': str(number)} for number in range(301)], kind='cve')
    client.session.post.assert_not_called()
    assert client.calls == 0


def test_authentication_failure_is_not_retried(monkeypatch):
    client = make_client(monkeypatch)
    client.session.post = Mock(return_value=response({}, status=401))
    with pytest.raises(GeminiAuthenticationError):
        curate_candidates(client, [{'id': 'one'}], kind='cve')
    assert client.calls == 1


def test_model_order_is_preserved_without_local_score_ranking(monkeypatch):
    client = make_client(monkeypatch)
    lower = dict(pick('one', 'tech'), scores=dict.fromkeys(SCORE_KEYS, 1))
    higher = dict(pick('two', 'tech'), scores=dict.fromkeys(SCORE_KEYS, 5))
    client.session.post = Mock(return_value=response(selection(lower, higher)))
    assert [row['id'] for row in curate_candidates(client, [{'id': 'one'}, {'id': 'two'}])['picks']] == [
        'one', 'two']


def test_cve_payload_preserves_provider_facts_and_bounds_description(monkeypatch):
    client = make_client(monkeypatch)
    client.session.post = Mock(return_value=response(selection(pick('CVE-2026-10000'))))
    cvss = {'score': 9.8, 'version': '3.1', 'source': 'nvd@nist.gov'}
    candidate = {'id': 'CVE-2026-10000', 'description': 'x' * 6000,
                 'products': [{'vendor': 'vendor', 'product': 'library'}],
                 'known_exploited': True, 'kev': {'added_at': '2026-10-03'}, 'cvss': cvss}
    curate_candidates(client, [candidate], kind='cve')
    sent = payload_candidates(client.session.post.call_args.kwargs)[0]
    assert len(sent['description']) == 1500
    assert sent['cvss'] == cvss
    assert sent['products'] == candidate['products']
    assert sent['known_exploited'] is True
    assert sent['kev'] == {'added_at': '2026-10-03'}
    assert candidate['cvss'] == cvss


def test_distinct_vulnerabilities_in_same_library_remain_separate_if_gemini_selects_both(monkeypatch):
    client = make_client(monkeypatch)
    candidates = [
        {'id': 'CVE-2026-10000', 'description': 'Library: authentication bypass',
         'products': [{'vendor': 'vendor', 'product': 'library'}], 'cvss': {'score': 9.8}},
        {'id': 'CVE-2026-10001', 'description': 'Library: memory corruption',
         'products': [{'vendor': 'vendor', 'product': 'library'}], 'cvss': {'score': 9.1}},
    ]
    client.session.post = Mock(return_value=response(selection(*(pick(row['id']) for row in candidates))))
    result = curate_candidates(client, candidates, kind='cve')
    assert len(result['picks']) == 2
    assert all(not row['related_ids'] for row in result['picks'])
    assert [row['cvss']['score'] for row in candidates] == [9.8, 9.1]


def test_rejected_cve_cannot_be_published_even_if_model_selects_it(monkeypatch):
    client = make_client(monkeypatch)
    client.session.post = Mock(return_value=response(selection(pick('CVE-2026-10000'))))
    with pytest.raises(GeminiResponseValidationError, match='Rejected'):
        curate_candidates(client, [{'id': 'CVE-2026-10000', 'vuln_status': 'Rejected'}], kind='cve')
    assert client.calls == 2


def test_empty_or_ineligible_pool_does_not_invent_representatives(monkeypatch):
    client = make_client(monkeypatch)
    client.session.post = Mock(return_value=response(selection(shortfall='적격 후보가 없습니다.')))
    assert curate_candidates(client, [])['picks'] == []
    assert client.calls == 0
    assert curate_candidates(client, [{'id': 'one'}]) == selection(shortfall='적격 후보가 없습니다.')


def test_all_empty_preliminary_groups_keep_gemini_shortfall_reason(monkeypatch):
    client = make_client(monkeypatch)
    client.session.post = Mock(return_value=response(selection(shortfall='관련 근거가 부족합니다.')))
    result = curate_candidates(client, [{'id': str(number)} for number in range(301)])
    assert result == selection(shortfall='관련 근거가 부족합니다.')
    assert client.calls == 2


def test_default_request_output_limit_does_not_change_existing_callers(monkeypatch):
    client = make_client(monkeypatch)
    client.session.post = Mock(return_value=response({}))
    client.request('test', {}, {'type': 'object'}, client.summary_model)
    assert client.session.post.call_args.kwargs['json']['generation_config']['max_output_tokens'] == 8192
