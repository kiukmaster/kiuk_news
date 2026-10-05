"""Gemini selects the day's representatives before expensive body/summary work.

Every candidate is screened. Bounded intermediate rounds reduce large feeds,
then Gemini makes the final ranking; Python never substitutes a popularity or
CVSS ranking. A failed round cannot return a partially curated public result.
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from .gemini import BudgetExceeded, GeminiResponseValidationError

# Leave room for the structured selection and the model's thinking. This must
# stay above the maximum 100 news representatives so every round shrinks.
CURATION_CHUNK_SIZE = 200
CURATION_ATTEMPTS = 2
CURATION_MODEL = 'gemini-3.8-flash'
NEWS_CATEGORIES = ('ai', 'security', 'tech', 'event', 'github')
SCORE_KEYS = ('social_impact', 'attention', 'issue_relevance')


def _check_options(kind: str, limit: int) -> None:
    if kind not in ('news', 'cve'):
        raise ValueError('Curation kind must be news or cve')
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError('Daily curation limit must be between 1 and 20')


def curation_max_calls(candidate_count: int, kind: str = 'news', limit: int = 20) -> int:
    """Physical call upper bound, including both attempts in every round."""
    _check_options(kind, limit)
    if type(candidate_count) is not int or candidate_count < 0:
        raise ValueError('Candidate count must be a nonnegative integer')
    capacity = limit * (len(NEWS_CATEGORIES) if kind == 'news' else 1)
    rounds, count = 0, candidate_count
    while count > CURATION_CHUNK_SIZE:
        full, tail = divmod(count, CURATION_CHUNK_SIZE)
        rounds += full + bool(tail)
        count = full * capacity + min(tail, capacity)
    return (rounds + bool(count)) * CURATION_ATTEMPTS


def _text(value: Any, limit: int) -> str:
    return str(value or '')[:limit]


def _compact_candidate(candidate: dict, kind: str, inherited: list[str]) -> dict:
    row = {'id': candidate['id'], 'published_at': candidate.get('published_at'),
           'observed_at': candidate.get('observed_at') or candidate.get('first_seen_at'),
           'url': _text(candidate.get('url'), 500)}
    if inherited:
        row['already_grouped_ids'] = inherited
    if kind == 'news':
        row.update({
            'kind': candidate.get('kind', 'news'),
            'title_original': _text(candidate.get('title_original'), 240),
            'title_ko': _text(candidate.get('title_ko'), 180),
            'excerpt': _text(candidate.get('excerpt') or candidate.get('description'), 1500),
            'summary_ko': _text(candidate.get('summary_ko'), 500),
            'source': _text(candidate.get('source'), 120),
            'category_hint': candidate.get('category_hint') or candidate.get('category'),
            'evidence_kind': candidate.get('evidence_kind'),
            'stars_today': candidate.get('stars_today'),
        })
    else:
        row.update({
            'description': _text(candidate.get('description'), 1500),
            'summary_ko': _text(candidate.get('summary_ko'), 500),
            'vuln_status': candidate.get('vuln_status'),
            'cvss': candidate.get('cvss'),
            'cvss_all': candidate.get('cvss_all', [])[:8],
        })
        # Only copy NVD/provider evidence. Missing CISA fields mean unknown,
        # never "not exploited". Keep provider scores separate from AI scores.
        for name in ('products', 'cpes', 'weaknesses', 'references', 'known_exploited', 'kev', 'cisa_kev',
                     'cisa_exploit_add', 'cisa_action_due', 'cisa_required_action',
                     'cisa_vulnerability_name'):
            value = candidate.get(name)
            if value is not None:
                row[name] = value[:30] if isinstance(value, list) else value
    return row


def _schema(kind: str, limit: int) -> dict:
    properties = {
        'id': {'type': 'string'},
        'scores': {'type': 'object', 'properties': {
            key: {'type': 'integer'} for key in SCORE_KEYS},
            'required': list(SCORE_KEYS), 'additionalProperties': False},
        # Google's supported schema subset omits maxLength/uniqueItems.
        # Enforce lengths and uniqueness in the semantic validator instead.
        'reason_ko': {'type': 'string'},
        'related_ids': {'type': 'array', 'items': {'type': 'string'}},
    }
    required = list(properties)
    if kind == 'news':
        properties['category'] = {'type': 'string', 'enum': list(NEWS_CATEGORIES)}
        required.append('category')
    return {'type': 'object', 'properties': {
        # Combining large maxItems and nested numeric bounds is rejected by
        # the live API with HTTP 400. All quotas/ranges are enforced below.
        'picks': {'type': 'array',
                  'items': {'type': 'object', 'properties': properties,
                            'required': required, 'additionalProperties': False}},
        'shortfall_reason_ko': {'type': 'string'}},
        'required': ['picks', 'shortfall_reason_ko'], 'additionalProperties': False}


def _allowed_categories(candidate: dict) -> set[str]:
    return {'github': {'github'}, 'event': {'event'}, 'paper': {'tech'}}.get(
        candidate.get('kind'), {'ai', 'security', 'tech'})


def _korean_reason(value: Any, maximum: int, allow_empty: bool = False) -> bool:
    if not isinstance(value, str) or len(value) > maximum:
        return False
    return (allow_empty and not value.strip()) or (bool(value.strip())
        and any('\uac00' <= char <= '\ud7a3' for char in value))


def _validate_selection(result: dict, candidates: list[dict], kind: str, limit: int) -> None:
    """Validate before any selection is exposed or inherited by another round."""
    try:
        picks = result['picks']
        if not isinstance(picks, list) or len(picks) > limit * (5 if kind == 'news' else 1):
            raise GeminiResponseValidationError('선정 개수 상한 초과')
        shortfall = result['shortfall_reason_ko']
        if not _korean_reason(shortfall, 500, allow_empty=bool(picks)):
            raise GeminiResponseValidationError('선정 부족 사유가 유효하지 않습니다')
        by_id = {candidate['id']: candidate for candidate in candidates}
        ids = [pick['id'] for pick in picks]
        if len(ids) != len(set(ids)) or not set(ids).issubset(by_id):
            raise GeminiResponseValidationError('선정 결과의 ID 누락·중복·변조')
        represented, categories = set(ids), Counter()
        for pick in picks:
            if not _korean_reason(pick['reason_ko'], 200):
                raise GeminiResponseValidationError('한국어 선정 이유가 유효하지 않습니다')
            scores = pick['scores']
            if (not isinstance(scores, dict) or set(scores) != set(SCORE_KEYS)
                    or any(type(scores[key]) is not int or not 0 <= scores[key] <= 5
                           for key in SCORE_KEYS)):
                raise GeminiResponseValidationError('선정 중요도 점수가 유효하지 않습니다')
            related = pick['related_ids']
            if (not isinstance(related, list) or len(related) != len(set(related))
                    or not set(related).issubset(by_id) or represented.intersection(related)):
                raise GeminiResponseValidationError('중복으로 묶은 ID가 유효하지 않습니다')
            represented.update(related)
            if kind == 'news':
                category = pick['category']
                if category not in _allowed_categories(by_id[pick['id']]):
                    raise GeminiResponseValidationError('기사 종류와 선정 카테고리가 일치하지 않습니다')
                categories[category] += 1
                if categories[category] > limit:
                    raise GeminiResponseValidationError('카테고리별 선정 개수 상한 초과')
            elif (by_id[pick['id']].get('rejected')
                  or str(by_id[pick['id']].get('vuln_status', '')).lower() in ('reject', 'rejected')):
                raise GeminiResponseValidationError('Rejected CVE는 선정할 수 없습니다')
    except (KeyError, TypeError, AttributeError):
        raise GeminiResponseValidationError('선정 응답 형식 불일치') from None


def _instruction(kind: str, limit: int, preliminary: bool) -> str:
    stage = ('대규모 후보의 중간 선발이며 통과한 대표는 다음 단계에서 다시 비교한다. '
             if preliminary else '오늘 보고서의 최종 선발이다. ')
    shared = (
        '입력 근거만으로 관련성과 의미상 중복을 판단하고 중요도순으로 picks를 반환하라. '
        'scores의 social_impact(사회적 파급력), attention(화제성), '
        'issue_relevance(현재 이슈성)를 각각 0~5 정수로 평가하며 세 기준을 동일한 비중으로 고려하라. '
        '파급력은 영향을 받는 사람·조직·생태계와 영향의 범위, 화제성은 입력에 확인된 관심·확산 근거, '
        '이슈성은 시의성·긴급성·새로운 변화와 독자의 대응 필요성을 뜻한다. '
        '실제 인기 수치·악용 여부·사회 반응을 추측하거나 만들지 말고 근거가 약하면 보수적으로 평가하라. '
        '자극적인 제목이나 홍보 문구만으로 고르지 말라. '
        '서로 다른 언어·매체가 같은 사건을 반복하면 대표 한 개를 남기고 '
        'related_ids에 이번 입력에 있는 동일 사건의 다른 ID를 넣어라. '
        '각 ID는 대표 또는 related_ids 중 한 곳에만 등장할 수 있다. '
        'already_grouped_ids는 앞 단계에서 묶은 원본 ID이며 이번 출력에 직접 넣지 말라. '
        'reason_ko는 근거에 기반한 짧은 한국어 한 문장, 80자 이내로 작성하라. '
        '후보별 분석이나 긴 검토 과정을 출력하지 말고 지정된 선정 결과 JSON만 간결하게 반환하라. '
        '후보 밖 ID는 금지한다. '
        '적격 후보가 부족하면 가능한 개수만 고르고 shortfall_reason_ko로 한국어 이유를 써라. '
        '하나도 고르지 않으면 반드시 이유를 써라. 충분하면 부족 이유는 빈 문자열로 써라. '
    )
    if kind == 'news':
        policy = (
            f'ai/security/tech/event/github 각 카테고리의 대표를 최대 {limit}개만 선정하라. '
            '일반 뉴스는 AI 모델·제품 ai, 공격·취약점·방어·AI 보안 security, 기타 컴퓨팅 신기술 tech다. '
            'kind=paper는 tech, kind=github는 github, kind=event는 event로만 분류하라. '
            '대학생·청년이 참여 가능한 국내 AI·보안·SW·데이터 해커톤·대회·행사 모집을 포함하라. '
            'as_of는 한국시간 평가 기준 시각이다. 그 시각과 공고 근거로 접수 마감이 확인된 행사는 제외하라. '
            '관련 없는 뉴스와 근거 없는 광고는 제외하라. '
        )
    else:
        policy = (
            f'오늘 공개된 CVE 대표를 최대 {limit}개만 선정하라. '
            '같은 제품·라이브러리라는 이유만으로 서로 다른 취약점을 중복 취급하지 말라. '
            '같은 제품이며 원인·취약 기능·공격 경로·패치 이슈가 실제로 같은 반복된 내용만 묶어라. '
            '독립적인 심각한 취약점은 별도 대표로 남길 수 있다. '
            'CPE·제품 정보가 없으면 설명의 확인 가능한 근거만 쓰며 관련성이 불확실하면 묶지 말라. '
            '공식 CVSS는 원본 심각도이며 중요도 scores와 별개다. 원본 점수를 수정하거나 새로 계산하지 말라. '
            'CISA KEV 악용 정보는 명시된 경우만 확인된 악용으로 취급하고 누락은 알 수 없음이다. '
            'Rejected 상태는 선정하지 말라. '
        )
    return stage + policy + shared


def curate_candidates(client, candidates: list[dict], kind: str = 'news',
                      model: str = CURATION_MODEL, limit: int = 20, as_of: str = '') -> dict:
    """Return a validated daily ranking and transitive semantic duplicate IDs.

    Reserve the whole tournament before its first call. All requests share the
    client's existing physical call counter, interval and 429 cooldown.
    """
    _check_options(kind, limit)
    ids = [candidate.get('id') for candidate in candidates]
    if any(not isinstance(ident, str) or not ident.strip() for ident in ids) or len(ids) != len(set(ids)):
        raise GeminiResponseValidationError('선정 입력의 ID 누락·중복')
    if not candidates:
        return {'picks': [], 'shortfall_reason_ko': '선정할 새 후보가 없습니다.'}
    required = curation_max_calls(len(candidates), kind, limit)
    if client.remaining < required:
        raise BudgetExceeded(f'전체 후보 선정에 필요한 API 호출 예산이 부족합니다 ({required}회 예약 필요)')
    originals = {candidate['id']: dict(candidate) for candidate in candidates}
    grouped = {ident: [] for ident in originals}
    finalists = list(originals.values())

    def select_round(group: list[dict], preliminary: bool) -> dict:
        data = {'as_of': as_of, 'candidates': [_compact_candidate(candidate, kind, grouped[candidate['id']])
                               for candidate in group]}
        result = client.request(_instruction(kind, limit, preliminary), data, _schema(kind, limit), model,
            attempts=CURATION_ATTEMPTS,
            validator=lambda result: _validate_selection(result, group, kind, limit),
            max_output_tokens=32768, thinking_level='low')
        # Defensively validate lightweight test/integration clients too.
        _validate_selection(result, group, kind, limit)
        output = {'picks': [], 'shortfall_reason_ko': result['shortfall_reason_ko']}
        for pick in result['picks']:
            expanded = list(grouped[pick['id']])
            for related_id in pick['related_ids']:
                expanded.append(related_id)
                expanded.extend(grouped[related_id])
            copied = dict(pick, scores=dict(pick['scores']), related_ids=expanded)
            output['picks'].append(copied)
        return output

    while len(finalists) > CURATION_CHUNK_SIZE:
        next_round = []
        empty_reasons = []
        for start in range(0, len(finalists), CURATION_CHUNK_SIZE):
            result = select_round(finalists[start:start + CURATION_CHUNK_SIZE], True)
            if not result['picks']:
                empty_reasons.append(result['shortfall_reason_ko'])
            for pick in result['picks']:
                grouped[pick['id']] = pick['related_ids']
                next_round.append(originals[pick['id']])
        finalists = next_round
        if not finalists:
            return {'picks': [], 'shortfall_reason_ko': ' '.join(dict.fromkeys(empty_reasons))[:500]}
    return select_round(finalists, False)
