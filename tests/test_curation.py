"""Daily selection is bounded, atomic and entirely offline in these tests."""
import copy
import json
from collections import Counter
from unittest.mock import Mock

import pytest
from jsonschema import ValidationError, validate

from digest.common import load_config
from digest.curation import (CURATION_CHUNK_SIZE, SCORE_KEYS, curate_candidates,
                             curation_max_calls, fixed_category, _schema, _normalize_selection)
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


def provider_topic(category):
    # event/github describe the final report field, not a provider topic.
    return 'tech' if category in ('event', 'github') else category


def assert_provider_topics(call):
    schema = call['json']['response_format']['schema']
    assert schema['properties']['picks']['items']['properties']['category']['enum'] == [
        'ai', 'security', 'tech']


@pytest.mark.parametrize('kind,counts', [
    ('news', {0: 0, 1: 2, 200: 2, 201: 6, 400: 6, 401: 12,
              557: 12, 600: 12, 601: 14, 607: 14, 1801: 42}),
    ('cve', {0: 0, 1: 2, 200: 2, 201: 6, 400: 6, 401: 8,
             557: 8, 600: 8, 601: 10, 607: 10, 4501: 54}),
])
def test_call_bound_counts_all_rounds_and_both_physical_attempts(kind, counts):
    for count, expected in counts.items():
        assert curation_max_calls(count, kind) == expected


@pytest.mark.parametrize('candidate_count', [557, 607, 1801])
def test_every_candidate_is_screened_and_final_news_cap_is_per_category(monkeypatch, candidate_count):
    client = make_client(monkeypatch)
    categories = ('ai', 'security', 'tech', 'event', 'github')
    candidates = []
    for number in range(candidate_count):
        category = categories[number % 5]
        candidates.append({'id': str(number), 'category_hint': category,
                           'kind': category if category in ('event', 'github') else 'news'})
    seen = set()

    def choose(*args, **kwargs):
        rows = payload_candidates(kwargs)
        assert_provider_topics(kwargs)
        assert len(rows) <= CURATION_CHUNK_SIZE
        seen.update(row['id'] for row in rows)
        counts, picks = Counter(), []
        for row in rows:
            category = row['category_hint']
            if counts[category] < 20:
                picks.append(pick(row['id'], provider_topic(category)))
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
    assert all(call.kwargs['json']['generation_config']['max_output_tokens'] == 32768
               for call in client.session.post.call_args_list)
    assert all(call.kwargs['json']['generation_config']['thinking_level'] == 'low'
               for call in client.session.post.call_args_list)
    assert candidates == original


@pytest.mark.parametrize('candidate_count,physical_limit', [(557, 12), (607, 14)])
def test_actual_failed_pool_sizes_stay_inside_budget_when_every_round_retries(
        monkeypatch, candidate_count, physical_limit):
    # Reproduce the two live backlog sizes, with a failed response and a valid
    # response for every group. The complete tournament must still fit exactly
    # in its advertised physical-call reservation and screen every original ID.
    client = make_client(monkeypatch, budget=physical_limit)
    categories = ('ai', 'security', 'tech', 'event', 'github')
    candidates = [{'id': str(number), 'category_hint': categories[number % 5],
                   'kind': categories[number % 5] if categories[number % 5] in ('event', 'github') else 'news'}
                  for number in range(candidate_count)]
    seen = set()

    def choose(*args, **kwargs):
        rows = payload_candidates(kwargs)
        assert_provider_topics(kwargs)
        assert len(rows) <= 200
        seen.update(row['id'] for row in rows)
        if client.calls % 2:
            return response(selection(pick('outside-current-group', 'ai')))
        counts, picks = Counter(), []
        for row in rows:
            category = row['category_hint']
            if counts[category] < 20:
                picks.append(pick(row['id'], provider_topic(category)))
                counts[category] += 1
        return response(selection(*picks))

    client.session.post = Mock(side_effect=choose)
    result = curate_candidates(client, candidates)
    assert seen == {row['id'] for row in candidates}
    assert Counter(row['category'] for row in result['picks']) == dict.fromkeys(categories, 20)
    assert client.calls == client.session.post.call_count == physical_limit
    assert curation_max_calls(candidate_count, 'news') == physical_limit
    assert client.remaining == 0


def test_later_preliminary_failure_cannot_expose_an_earlier_valid_selection(monkeypatch):
    client = make_client(monkeypatch)
    candidates = [{'id': str(number)} for number in range(557)]
    original = copy.deepcopy(candidates)

    def choose(*args, **kwargs):
        rows = payload_candidates(kwargs)
        if client.calls == 1:
            return response(selection(pick(rows[0]['id'], 'ai')))
        # ID zero was valid in the earlier group, but is outside this group.
        # Neither this retry nor the completed earlier result may be returned.
        return response(selection(pick('0', 'ai')))

    client.session.post = Mock(side_effect=choose)
    with pytest.raises(GeminiResponseValidationError, match='ID'):
        curate_candidates(client, candidates)
    assert client.calls == client.session.post.call_count == 3
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


def test_category_quota_preserves_gemini_order_without_requesting_another_ranking(monkeypatch):
    client = make_client(monkeypatch)
    candidates = [{'id': str(number)} for number in range(21)]
    raw = selection(*(pick(row['id'], 'ai') for row in candidates))
    client.session.post = Mock(return_value=response(raw))
    result = curate_candidates(client, candidates)
    assert len(result['picks']) == 20
    assert [row['id'] for row in result['picks']] == [row['id'] for row in candidates[:20]]
    assert client.calls == 1
    assert len(raw['picks']) == 21


@pytest.mark.parametrize('invalid_last', [
    pick('outside', 'ai'),
    pick('0', 'ai'),
    dict(pick('20', 'ai'), scores=dict.fromkeys(SCORE_KEYS, True)),
    dict(pick('20', 'ai'), reason_ko='Invalid English reason'),
    pick('20', 'ai', related=['outside']),
    pick('20', 'ai', related=['0']),
    pick('20', 'ai', related=['outside', 'outside']),
    pick('20', 'github'),
    dict(pick('20', 'ai'), category='not-a-report-category'),
    dict(pick('20', 'ai'), unexpected_field='not-in-schema'),
])
def test_invalid_twenty_first_pick_is_not_hidden_by_the_daily_quota(monkeypatch, invalid_last):
    client = make_client(monkeypatch)
    candidates = [{'id': str(number)} for number in range(21)]
    raw = selection(*(pick(str(number), 'ai') for number in range(20)), invalid_last)
    original = copy.deepcopy(raw)
    client.session.post = Mock(return_value=response(raw))
    # JSON Schema can reject malformed types before the semantic callback;
    # either failure path must reject the whole response instead of cutting it.
    with pytest.raises(GeminiError):
        curate_candidates(client, candidates)
    assert client.calls == client.session.post.call_count == 2
    assert raw == original


def test_total_raw_one_hundred_pick_limit_cannot_be_hidden_by_category_cuts(monkeypatch):
    client = make_client(monkeypatch)
    candidates = [{'id': str(number)} for number in range(101)]
    raw = selection(*(pick(str(number), ('ai', 'security', 'tech')[number % 3])
                      for number in range(101)))
    client.session.post = Mock(return_value=response(raw))
    with pytest.raises(GeminiResponseValidationError, match='상한') as failure:
        curate_candidates(client, candidates)
    assert failure.value.retry_code == 'COUNT'
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


@pytest.mark.parametrize('kind,category,canonical', [
    ('paper', 'ai', 'tech'), ('paper', 'security', 'tech'),
    ('event', 'tech', 'event'), ('github', 'security', 'github'),
])
def test_fixed_source_kind_controls_section_without_discarding_gemini_selection(
        monkeypatch, kind, category, canonical):
    client = make_client(monkeypatch)
    raw = selection(pick('one', category))
    original = copy.deepcopy(raw)
    client.session.post = Mock(return_value=response(raw))
    result = curate_candidates(client, [{'id': 'one', 'kind': kind}])
    assert result['picks'] == [dict(raw['picks'][0], category=canonical)]
    assert client.calls == 1
    assert raw == original
    sent = payload_candidates(client.session.post.call_args.kwargs)[0]
    assert sent['fixed_category'] == canonical
    assert sent['allowed_categories'] == ['ai', 'security', 'tech']
    assert_provider_topics(client.session.post.call_args.kwargs)


@pytest.mark.parametrize('category', ['github', 'event'])
def test_ordinary_article_invalid_topic_remains_strict_and_has_safe_retry_code(monkeypatch, category):
    client = make_client(monkeypatch)
    client.session.post = Mock(return_value=response(selection(pick('one', category))))
    with pytest.raises(GeminiResponseValidationError, match='스키마') as failure:
        curate_candidates(client, [{'id': 'one', 'kind': 'article'}])
    assert failure.value.retry_code == 'GENERIC'
    assert client.calls == client.session.post.call_count == 2
    sent = payload_candidates(client.session.post.call_args.kwargs)[0]
    assert sent['fixed_category'] is None
    assert sent['allowed_categories'] == ['ai', 'security', 'tech']


def test_fifty_one_papers_in_three_model_topics_use_first_twenty_model_ranked_picks(monkeypatch):
    client = make_client(monkeypatch)
    candidates = [{'id': str(number), 'kind': 'paper'} for number in range(51)]
    raw = selection(*(dict(pick(str(number), ('ai', 'security', 'tech')[number % 3]),
                           scores=dict.fromkeys(SCORE_KEYS, 1 if number < 20 else 5))
                      for number in range(51)))
    original_candidates, original_response = copy.deepcopy(candidates), copy.deepcopy(raw)
    client.session.post = Mock(return_value=response(raw))
    result = curate_candidates(client, candidates)
    assert [row['id'] for row in result['picks']] == [str(number) for number in range(20)]
    assert all(row['category'] == 'tech' and row['scores'] == dict.fromkeys(SCORE_KEYS, 1)
               for row in result['picks'])
    assert client.calls == 1
    assert candidates == original_candidates and raw == original_response


def test_live_six_hundred_thirty_four_pool_screens_every_candidate_with_papers_returned_as_ai(monkeypatch):
    client = make_client(monkeypatch)
    # The failed day's actual structural distribution: 456 papers, 169 articles,
    # 9 repositories. Reproduce topic-based AI labels for the selected papers.
    candidates = []
    for number in range(634):
        kind = 'paper' if number < 456 else ('article' if number < 625 else 'github')
        category = {'paper': 'tech', 'github': 'github'}.get(kind, ('ai', 'security', 'tech')[number % 3])
        candidates.append({'id': str(number), 'kind': kind, 'category_hint': category})
    original = copy.deepcopy(candidates)
    seen = set()

    def choose(*args, **kwargs):
        rows = payload_candidates(kwargs)
        assert_provider_topics(kwargs)
        seen.update(row['id'] for row in rows)
        counts, picks = Counter(), []
        for row in rows:
            canonical = fixed_category(row) or row['category_hint']
            topic = 'ai' if row['kind'] == 'paper' else provider_topic(row['category_hint'])
            assert row['allowed_categories'] == ['ai', 'security', 'tech']
            if counts[canonical] < 20:
                picks.append(pick(row['id'], topic))
                counts[canonical] += 1
        return response(selection(*picks))

    client.session.post = Mock(side_effect=choose)
    result = curate_candidates(client, candidates)
    assert seen == {row['id'] for row in candidates}
    assert all(count <= 20 for count in Counter(row['category'] for row in result['picks']).values())
    by_id = {candidate['id']: candidate for candidate in candidates}
    selected_papers = [row for row in result['picks'] if by_id[row['id']]['kind'] == 'paper']
    assert selected_papers and all(row['category'] == 'tech' for row in selected_papers)
    assert client.calls <= curation_max_calls(634, 'news')
    assert candidates == original


def test_provider_topics_restore_all_five_sections_without_shared_topic_quota(monkeypatch):
    client = make_client(monkeypatch)
    candidates, picks = [], []
    for category, kind, topic in [('ai', 'article', 'ai'), ('security', 'article', 'security'),
                                  ('tech', 'paper', 'ai'), ('event', 'event', 'ai'),
                                  ('github', 'github', 'ai')]:
        for number in range(20):
            ident = f'{category}-{number}'
            candidates.append({'id': ident, 'kind': kind, 'category_hint': category})
            picks.append(pick(ident, topic))
    raw = selection(*picks)
    original = copy.deepcopy(raw)
    client.session.post = Mock(return_value=response(raw))
    result = curate_candidates(client, candidates)
    assert Counter(row['category'] for row in raw['picks']) == {'ai': 80, 'security': 20}
    assert Counter(row['category'] for row in result['picks']) == dict.fromkeys(
        ('ai', 'security', 'tech', 'event', 'github'), 20)
    assert [row['id'] for row in result['picks']] == [row['id'] for row in raw['picks']]
    assert client.calls == client.session.post.call_count == 1
    assert_provider_topics(client.session.post.call_args.kwargs)
    validate(result, _schema('news', 20))
    assert raw == original


@pytest.mark.parametrize('kind,category', [('event', 'event'), ('github', 'github')])
def test_provider_schema_rejects_fixed_section_names_even_for_fixed_source_kinds(
        monkeypatch, kind, category):
    client = make_client(monkeypatch)
    raw = selection(pick('fixed', category))
    # Canonical stored selections retain their five-section format, while
    # actual HTTP model responses must use only the provider's three topics.
    validate(raw, _schema('news', 20))
    with pytest.raises(ValidationError):
        validate(raw, _schema('news', 20, topic_only=True))
    client.session.post = Mock(return_value=response(raw))
    with pytest.raises(GeminiResponseValidationError) as failure:
        curate_candidates(client, [{'id': 'fixed', 'kind': kind}])
    assert failure.value.retry_code == 'GENERIC'
    assert client.calls == client.session.post.call_count == 2


def test_canonical_normalizer_preserves_saved_fixed_fields_but_checks_every_id():
    candidates = [{'id': 'event', 'kind': 'event'}, {'id': 'repo', 'kind': 'github'}]
    raw = selection(pick('event', 'event'), pick('repo', 'github'))
    assert _normalize_selection(raw, candidates, 'news', 20) == raw
    invalid = selection(pick('event', 'event'), pick('outside', 'github'))
    with pytest.raises(GeminiResponseValidationError) as failure:
        _normalize_selection(invalid, candidates, 'news', 20)
    assert failure.value.retry_code == 'IDS'


def test_fixed_category_normalization_preserves_transitive_duplicate_groups_between_rounds(monkeypatch):
    client = make_client(monkeypatch)
    candidates = [{'id': str(number), 'kind': 'paper'} for number in range(201)]
    client.session.post = Mock(side_effect=[
        response(selection(pick('0', 'ai', related=['1']))),
        response(selection(pick('200', 'security'))),
        response(selection(pick('200', 'ai', related=['0']))),
    ])
    result = curate_candidates(client, candidates)
    assert result['picks'][0]['id'] == '200'
    assert result['picks'][0]['category'] == 'tech'
    assert result['picks'][0]['related_ids'] == ['0', '1']
    assert client.calls == 3


def test_quota_removed_paper_groups_do_not_leak_into_the_retained_representative(monkeypatch):
    client = make_client(monkeypatch)
    candidates = [{'id': str(number), 'kind': 'paper'} for number in range(201)]
    preliminary = selection(*(pick(str(number), ('ai', 'security', 'tech')[number % 3],
                                   related=[str(number + 100)]) for number in range(50)))
    client.session.post = Mock(side_effect=[
        response(preliminary), response(selection(pick('200', 'ai'))),
        response(selection(pick('200', 'security', related=['0']))),
    ])
    result = curate_candidates(client, candidates)
    final_input = payload_candidates(client.session.post.call_args_list[-1].kwargs)
    assert [row['id'] for row in final_input] == [str(number) for number in range(20)] + ['200']
    assert result['picks'][0]['related_ids'] == ['0', '100']
    assert not set(str(number) for number in range(20, 50)) & set(result['picks'][0]['related_ids'])


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
