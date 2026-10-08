"""The independent latest feed follows verified publication clocks, not scores."""
import copy
from datetime import datetime

import pytest

from digest.common import KST
from digest.gemini import BudgetExceeded, GeminiResponseValidationError
from digest.latest import latest_eligible, select_latest_candidates

NOW = datetime(2026, 10, 9, 13, 0, tzinfo=KST)


def article(ident, published='2026-10-09T12:00:00+09:00', **kwargs):
    return {'id': ident, 'kind': 'article', 'published_at': published,
            'url': f'https://example.com/{ident}',
            'title_original': f'Article number {ident} discusses a different security incident', **kwargs}


class Client:
    remaining = 80

    def __init__(self, result=None):
        self.result = result
        self.calls = []

    def request(self, instruction, data, schema, model, **kwargs):
        self.calls.append((instruction, copy.deepcopy(data), schema, model, kwargs))
        result = self.result if self.result is not None else {
            'picks': [{'id': row['id'], 'related_ids': []} for row in data['candidates']],
            'shortfall_reason_ko': '',
        }
        kwargs['validator'](result)
        return result


@pytest.mark.parametrize('published,expected', [
    ('2026-10-08T00:00:00+09:00', True),
    ('2026-10-07T23:59:59+09:00', False),
    ('2026-10-09T13:00:00+09:00', True),
    ('2026-10-09T13:00:00.000001+09:00', False),
    ('2026-10-07T15:00:00Z', True),
    ('2026-10-08T23:00:00-04:00', True),
    ('2026-10-08T00:00:00', True),
    ('Fri, 09 Oct 2026 04:00:00 GMT', True),
    ('2026-10-07', False), ('bad-clock', False), ('', False), (None, False),
])
def test_verified_publication_window_kst_yesterday_midnight_through_now(published, expected):
    row = article('clock', published, observed_at=NOW.isoformat())
    assert latest_eligible(row, NOW) is expected


@pytest.mark.parametrize('kind,expected', [
    ('article', True), ('event', True), ('paper', False), ('github', False),
    ('cve', False), ('news', False), (None, False),
])
def test_excludes_non_article_or_event_sources(kind, expected):
    assert latest_eligible(article('kind', kind=kind), NOW) is expected


def test_observation_time_does_not_create_a_publication_date():
    assert not latest_eligible(article('undated', None, observed_at=NOW.isoformat(),
                                      first_seen_at=NOW.isoformat()), NOW)
    assert latest_eligible(article('naive-now'), NOW.replace(tzinfo=None))


def test_only_twenty_newest_are_considered_not_older_high_importance_rows():
    rows = [article(str(hour), f'2026-10-08T{hour:02}:00:00+09:00',
                    scores={'social_impact': 5 if hour < 4 else 0}) for hour in range(24)]
    client = Client()
    result = select_latest_candidates(client, rows, NOW)
    assert result['candidate_count'] == 24
    assert result['considered_count'] == 20
    assert [pick['id'] for pick in result['picks']] == [str(hour) for hour in range(23, 3, -1)]
    instruction, data, _, model, options = client.calls[0]
    assert [row['id'] for row in data['candidates']] == [str(hour) for hour in range(23, 3, -1)]
    assert '점수나 순위는 사용하지 말라' in instruction
    assert all('scores' not in row for row in data['candidates'])
    assert model == 'gemini-3.8-flash'
    assert options['attempts'] == 2
    assert options['max_output_tokens'] == 8192
    assert options['thinking_level'] == 'low'


def test_equal_publication_dates_sort_by_id_stably_and_model_order_is_not_public_order():
    client = Client({'picks': [{'id': 'z', 'related_ids': []}, {'id': 'a', 'related_ids': []}],
                     'shortfall_reason_ko': ''})
    result = select_latest_candidates(client, [article('z'), article('a')], NOW)
    assert [row['id'] for row in client.calls[0][1]['candidates']] == ['a', 'z']
    assert [row['id'] for row in result['picks']] == ['a', 'z']


def test_candidate_ids_are_copied_exactly_instead_of_text_truncated():
    ident = 'article-reference-' + 'x' * 250
    client = Client()
    result = select_latest_candidates(client, [article(ident)], NOW)
    assert client.calls[0][1]['candidates'][0]['id'] == ident
    assert result['picks'][0]['id'] == ident


def test_exact_id_url_and_long_normalized_title_duplicates_keep_newest_before_cutoff():
    rows = [article('newest', title_original='Same latest security incident title',
                    url='https://example.com/shared?utm_source=new'),
            article('old-title', '2026-10-09T11:00:00+09:00',
                    title_original='Same latest, security Incident Title!'),
            article('old-url', '2026-10-09T10:00:00+09:00',
                    url='https://example.com/shared?utm_source=old'),
            article('newest', '2026-10-09T09:00:00+09:00'),
            article('independent', '2026-10-09T08:00:00+09:00')]
    original = copy.deepcopy(rows)
    result = select_latest_candidates(Client(), rows, NOW)
    assert result['candidate_count'] == 5
    assert result['considered_count'] == 2
    assert [row['id'] for row in result['picks']] == ['newest', 'independent']
    assert rows == original


def test_short_titles_do_not_collapse_independent_articles():
    result = select_latest_candidates(Client(), [article('a', title_original='News'),
                                                article('b', title_original='News')], NOW)
    assert len(result['picks']) == 2


def test_semantic_duplicate_uses_newest_representative_and_copies_model_output():
    rows = [article('old', '2026-10-09T10:00:00+09:00'), article('new')]
    selected = {'picks': [{'id': 'new', 'related_ids': ['old']}],
                'shortfall_reason_ko': '같은 사건의 중복 기사를 제외했습니다.'}
    original_rows, original_output = copy.deepcopy(rows), copy.deepcopy(selected)
    result = select_latest_candidates(Client(selected), rows, NOW)
    assert result['picks'] == selected['picks']
    result['picks'][0]['related_ids'].append('later-mutation')
    assert rows == original_rows
    assert selected == original_output


@pytest.mark.parametrize('picks,code', [
    ([{'id': 'unknown', 'related_ids': []}], 'IDS'),
    ([{'id': 'new', 'related_ids': []}, {'id': 'new', 'related_ids': []}], 'IDS'),
    ([{'id': 'new', 'related_ids': ['unknown']}], 'DUPLICATES'),
    ([{'id': 'new', 'related_ids': ['new']}], 'DUPLICATES'),
    ([{'id': 'new', 'related_ids': ['old', 'old']}], 'DUPLICATES'),
    ([{'id': 'new', 'related_ids': ['old']}, {'id': 'old', 'related_ids': []}], 'DUPLICATES'),
    ([{'id': 'old', 'related_ids': ['new']}], 'DUPLICATES'),
    ([{'id': 'new', 'related_ids': ['third']}, {'id': 'old', 'related_ids': ['third']}], 'DUPLICATES'),
])
def test_complete_strict_id_and_duplicate_validation(picks, code):
    client = Client({'picks': picks, 'shortfall_reason_ko': ''})
    with pytest.raises(GeminiResponseValidationError) as exc:
        select_latest_candidates(client, [article('new'),
            article('old', '2026-10-09T11:00:00+09:00'),
            article('third', '2026-10-09T10:00:00+09:00')], NOW)
    assert exc.value.retry_code == code


@pytest.mark.parametrize('reason', ['', 'No relevant articles', '가' * 501])
def test_empty_model_selection_requires_bounded_korean_reason(reason):
    with pytest.raises(GeminiResponseValidationError) as exc:
        select_latest_candidates(Client({'picks': [], 'shortfall_reason_ko': reason}),
                                 [article('new')], NOW)
    assert exc.value.retry_code == 'KOREAN_REASON'


def test_empty_model_selection_accepts_korean_explanation():
    result = select_latest_candidates(Client({'picks': [],
        'shortfall_reason_ko': '확인된 마감 행사와 무관한 기사를 제외했습니다.'}), [article('new')], NOW)
    assert result['picks'] == []
    assert result['considered_count'] == 1


def test_no_eligible_candidates_needs_no_api_budget_or_call():
    client = Client()
    client.remaining = 0
    result = select_latest_candidates(client, [article('old', '2026-10-07')], NOW)
    assert result['picks'] == []
    assert result['candidate_count'] == result['considered_count'] == 0
    assert '어제' in result['shortfall_reason_ko']
    assert client.calls == []


def test_reserves_both_physical_attempts_before_first_call():
    client = Client()
    client.remaining = 1
    with pytest.raises(BudgetExceeded):
        select_latest_candidates(client, [article('new')], NOW)
    assert client.calls == []


@pytest.mark.parametrize('limit', [0, 21, True, 1.0])
def test_invalid_feed_limits_are_rejected(limit):
    with pytest.raises(ValueError):
        select_latest_candidates(Client(), [], NOW, limit=limit)


def test_over_limit_model_output_is_rejected_without_silently_dropping_rows():
    selected = {'picks': [{'id': 'a', 'related_ids': []}, {'id': 'b', 'related_ids': []}],
                'shortfall_reason_ko': ''}
    with pytest.raises(GeminiResponseValidationError) as exc:
        select_latest_candidates(Client(selected), [article('a'), article('b')], NOW, limit=1)
    assert exc.value.retry_code == 'COUNT'


def test_event_deadline_must_be_provided_not_invented():
    client = Client()
    select_latest_candidates(client, [article('event', kind='event', region='KR',
        excerpt='접수 마감: 2026년 10월 8일')], NOW)
    instruction, data, *_ = client.calls[0]
    assert '현재 마감이 확인된 경우 제외하라' in instruction
    assert '날짜를 추정하거나 만들어 제외하지 말라' in instruction
    assert data['candidates'][0]['excerpt'] == '접수 마감: 2026년 10월 8일'
