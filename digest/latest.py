"""Select a separate recent feed by publication time, without popularity ranking."""
from __future__ import annotations

from datetime import datetime, timedelta

from jsonschema import ValidationError, validate

from .common import KST, canonical_url, parse_date, text_key
from .gemini import BudgetExceeded, GeminiResponseValidationError

LATEST_MODEL = 'gemini-3.8-flash'
LATEST_ATTEMPTS = 2


def _local_now(now: datetime) -> datetime:
    if not isinstance(now, datetime):
        raise TypeError('Latest feed now must be a datetime')
    return (now.replace(tzinfo=KST) if now.tzinfo is None else now.astimezone(KST))


def _published(item: dict) -> datetime | None:
    try:
        return parse_date(item.get('published_at'), default_tz=KST)
    except (TypeError, ValueError, OverflowError):
        return None


def latest_eligible(item: dict, now: datetime) -> bool:
    """Require a news/event publication from KST yesterday midnight until now.

    Discovery timestamps cannot substitute for a missing publication timestamp.
    Papers, CVEs and GitHub popularity snapshots are not articles in this feed.
    """
    if item.get('kind') not in ('article', 'event'):
        return False
    published = _published(item)
    if published is None:
        return False
    local_now = _local_now(now)
    start = (local_now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start <= published <= local_now


def _schema() -> dict:
    return {'type': 'object', 'properties': {
        'picks': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'id': {'type': 'string'},
            'related_ids': {'type': 'array', 'items': {'type': 'string'}},
        }, 'required': ['id', 'related_ids'], 'additionalProperties': False}},
        'shortfall_reason_ko': {'type': 'string'},
    }, 'required': ['picks', 'shortfall_reason_ko'], 'additionalProperties': False}


def _korean_reason(value, allow_empty: bool) -> bool:
    return (isinstance(value, str) and len(value) <= 500
            and ((allow_empty and not value.strip())
                 or (bool(value.strip()) and any('\uac00' <= char <= '\ud7a3' for char in value))))


def _validate_selection(result: dict, candidates: list[dict], limit: int) -> None:
    try:
        validate(result, _schema())
    except ValidationError:
        raise GeminiResponseValidationError('최신 선정 응답 JSON 형식 불일치') from None
    picks = result['picks']
    if len(picks) > limit:
        raise GeminiResponseValidationError('최신 선정 개수 상한 초과', retry_code='COUNT')
    if not _korean_reason(result['shortfall_reason_ko'], allow_empty=bool(picks)):
        raise GeminiResponseValidationError('최신 선정 부족 사유가 유효하지 않습니다',
                                             retry_code='KOREAN_REASON')
    by_id = {row['id']: row for row in candidates}
    ids = [pick['id'] for pick in picks]
    if len(ids) != len(set(ids)) or not set(ids).issubset(by_id):
        raise GeminiResponseValidationError('최신 선정 ID 누락·중복·변조', retry_code='IDS')
    represented = set(ids)
    for pick in picks:
        related = pick['related_ids']
        if (len(related) != len(set(related)) or not set(related).issubset(by_id)
                or represented.intersection(related)):
            raise GeminiResponseValidationError('최신 중복 ID가 유효하지 않습니다',
                                                 retry_code='DUPLICATES')
        published = _published(by_id[pick['id']])
        if any(published < _published(by_id[ident]) for ident in related):
            raise GeminiResponseValidationError('최신 중복 대표가 관련 기사보다 오래되었습니다',
                                                 retry_code='DUPLICATES')
        represented.update(related)


def _compact(candidate: dict) -> dict:
    return {name: str(candidate.get(name) or '')[:maximum] for name, maximum in (
        ('kind', 30), ('published_at', 100), ('url', 500),
        ('title_original', 240), ('title_ko', 180), ('source', 120),
        ('region', 30), ('category_hint', 30), ('summary_ko', 500),
    )} | {'id': candidate['id'],
          'excerpt': str(candidate.get('excerpt') or candidate.get('description') or '')[:1500]}


def select_latest_candidates(client, candidates: list[dict], now: datetime,
                             limit: int = 20, model: str = LATEST_MODEL) -> dict:
    """Screen the latest twenty eligible inputs, then keep actual date order.

    Gemini checks relevance, expired events and semantic duplicates; it never
    promotes an older article for importance. A failed model call raises rather
    than returning a partially validated selection.
    """
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError('Latest feed limit must be between 1 and 20')
    local_now = _local_now(now)
    eligible = [row for row in candidates if latest_eligible(row, local_now)]
    if any(not isinstance(row.get('id'), str) or not row['id'].strip() for row in eligible):
        raise GeminiResponseValidationError('최신 선정 입력 ID 누락·변조', retry_code='IDS')
    # Stable ID order for equal publication timestamps; no importance heuristic.
    ordered = sorted(eligible, key=lambda row: row['id'])
    ordered.sort(key=_published, reverse=True)
    unique, seen_ids, seen_urls, seen_titles = [], set(), set(), set()
    for row in ordered:
        try:
            url = canonical_url(row.get('url', ''))
        except (TypeError, ValueError):
            url = None
        title = text_key(str(row.get('title_original') or row.get('title_ko') or ''))
        dedup_title = title if len(title) >= 15 else None
        duplicate = (row['id'] in seen_ids or (url is not None and url in seen_urls)
                     or (dedup_title is not None and dedup_title in seen_titles))
        seen_ids.add(row['id'])
        if url is not None:
            seen_urls.add(url)
        if dedup_title is not None:
            seen_titles.add(dedup_title)
        if not duplicate:
            unique.append(row)
    considered = unique[:limit]
    metadata = {'candidate_count': len(eligible), 'considered_count': len(considered)}
    if not considered:
        return {'picks': [], 'shortfall_reason_ko': '어제부터 현재까지 발행 시각이 확인된 최신 기사가 없습니다.',
                **metadata}
    if client.remaining < LATEST_ATTEMPTS:
        raise BudgetExceeded('최신 기사 검증에 필요한 API 호출 예산이 부족합니다 (2회 예약 필요)')
    instruction = (
        f'독립 최신 기사 목록을 최대 {limit}개 선정하라. 입력은 원본 발행 시각이 최신인 순서이며 '
        '중요도·파급력·화제성 점수나 순위는 사용하지 말라. '
        'AI·사이버보안·컴퓨팅 기술 뉴스와 국내 대회·해커톤·대학생 행사에 관련된 실제 기사만 남겨라. '
        '관련성이 있고 마감이 확인되지 않은 서로 다른 사건의 입력 기사는 모두 남겨라. '
        '낮은 중요도나 관심도를 이유로 임의로 제외하지 말라. '
        '논문·CVE·GitHub 인기 저장소는 입력에 없으며 선정하지 말라. '
        '같은 사건의 의미상 중복은 제공된 발행 시각이 가장 최근인 기사를 대표로 남기고 '
        '나머지 ID만 related_ids에 묶어라. 서로 다른 사건을 같은 회사·제품이라는 이유로 묶지 말라. '
        '행사는 제공된 공고·접수 마감 근거로 현재 마감이 확인된 경우 제외하라. '
        '마감 근거가 없으면 날짜를 추정하거나 만들어 제외하지 말라. '
        '제외나 중복으로 부족해도 입력보다 오래된 기사를 새로 만들거나 다른 ID로 채우지 말라. '
        'ID는 입력에서 그대로 복사하고 대표·관련 ID 전체가 서로 겹치지 않게 하라. '
        '선정할 기사가 없으면 shortfall_reason_ko에 짧은 한국어 사유를 반드시 쓰고, '
        '기사 수가 충분하면 빈 문자열을 허용한다. 지정 JSON 형식만 반환하라.'
    )
    result = client.request(instruction,
        {'as_of': local_now.isoformat(), 'candidates': [_compact(row) for row in considered]},
        _schema(), model, attempts=LATEST_ATTEMPTS,
        validator=lambda result: _validate_selection(result, considered, limit),
        max_output_tokens=8192, thinking_level='low')
    _validate_selection(result, considered, limit)
    by_id = {row['id']: row for row in considered}
    picks = [{'id': pick['id'], 'related_ids': list(pick['related_ids'])}
             for pick in result['picks']]
    picks.sort(key=lambda pick: pick['id'])
    picks.sort(key=lambda pick: _published(by_id[pick['id']]), reverse=True)
    return {'picks': picks, 'shortfall_reason_ko': result['shortfall_reason_ko'], **metadata}
