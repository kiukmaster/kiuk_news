"""Official NVD CVE evidence and separately labeled Gemini editorial selection.

Date basis: NVD `published` (NVD publication, NOT CVE reservation, discovery,
lastModified or necessarily CVE Program first publication). All grouping is KST.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Callable

import requests

from .common import KST, canonical_url, parse_date
from .gemini import GeminiError, GeminiAuthenticationError, GeminiResponseValidationError

ENDPOINT = 'https://services.nvd.nist.gov/rest/json/cves/2.0'
CVE_ID = re.compile(r'CVE-\d{4}-\d{4,}')
VERSIONS = (('cvssMetricV40', '4.0'), ('cvssMetricV31', '3.1'),
            ('cvssMetricV30', '3.0'), ('cvssMetricV2', '2.0'))
SEVERITIES = {'NONE', 'LOW', 'MEDIUM', 'HIGH', 'CRITICAL', 'UNKNOWN'}
CURATION_SCORE_KEYS = {'social_impact', 'attention', 'issue_relevance'}
CURATION_FIELDS = ('curation', 'related_ids', 'related_cves')
def summary_schema(expected_ids: set[str]) -> dict:
    return {'type': 'object', 'properties': {'cves': {
        'type': 'array', 'minItems': len(expected_ids), 'maxItems': len(expected_ids),
        'items': {'type': 'object', 'properties': {
            'id': {'type': 'string', 'enum': sorted(expected_ids)},
            'summary_ko': {'type': 'string'}},
            'required': ['id', 'summary_ko']}}}, 'required': ['cves']}


def validate_summaries(response: dict, expected_ids: set[str]) -> None:
    """Reject the whole response before changing any CVE or saving a checkpoint."""
    try:
        answers = response['cves']
        ids = [answer['id'] for answer in answers]
        if len(set(ids)) != len(ids) or set(ids) != expected_ids:
            raise GeminiResponseValidationError('CVE 요약의 ID 누락·중복·변조')
        for answer in answers:
            text = answer['summary_ko'].strip()
            if not text or len(text) > 500 or not re.search(r'[가-힣]', text):
                raise GeminiResponseValidationError('CVE 한국어 요약 형식 불일치')
    except (KeyError, TypeError, AttributeError):
        raise GeminiResponseValidationError('CVE 요약 응답 형식 불일치') from None


class NvdError(RuntimeError):
    pass


def query_window(now: datetime, keep_days: int):
    if now.tzinfo is None:
        raise ValueError('CVE 수집 시각에는 시간대가 필요합니다')
    if not 1 <= keep_days <= 120:
        raise ValueError('NVD 조회 범위는 1~120일이어야 합니다')
    local = now.astimezone(KST)
    start = (local - timedelta(days=keep_days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, local


def utc_parameter(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


class NvdClient:
    """Fixed HTTPS API endpoint; no auth forwarded to redirects or article hosts."""
    def __init__(self, cfg: dict, session=None):
        self.session = session or requests.Session()
        self.session.trust_env = False
        self.key = os.environ.get('NVD_API_KEY', '').strip()
        self.user_agent = cfg['user_agent']
        self.timeout = int(cfg.get('nvd_timeout_seconds', 35))
        # 6.2 seconds also respects the unauthenticated 5 requests / 30s limit.
        self.interval = max(6.2, float(cfg.get('nvd_interval_seconds', 6.2)))
        self.last_request = 0.0

    def get_page(self, params: dict) -> dict:
        headers = {'User-Agent': self.user_agent, 'Accept': 'application/json'}
        if self.key:
            headers['apiKey'] = self.key
        for attempt in range(3):
            gap = self.last_request + self.interval - time.monotonic()
            if gap > 0:
                time.sleep(gap)
            self.last_request = time.monotonic()
            try:
                with self.session.get(ENDPOINT, params=params, headers=headers,
                                      timeout=(10, self.timeout), stream=True,
                                      allow_redirects=False) as response:
                    if response.status_code in (429, 500, 502, 503, 504):
                        if attempt == 2:
                            raise NvdError(f'NVD HTTP {response.status_code}: 요청 제한 또는 일시적 장애')
                        try:
                            pause = float(response.headers.get('Retry-After', '0'))
                        except ValueError:
                            pause = 0
                        time.sleep(min(60, max(pause, 8 * 2 ** attempt)))
                        continue
                    if response.status_code != 200:
                        raise NvdError(f'NVD HTTP {response.status_code}: API 키·요청 한도·응답 상태 확인')
                    chunks, size, started = [], 0, time.monotonic()
                    for block in response.iter_content(65536):
                        size += len(block)
                        if size > 40_000_000 or time.monotonic() - started > 90:
                            raise NvdError('NVD 응답 크기 또는 수신 시간 제한 초과')
                        chunks.append(block)
                    payload = json.loads(b''.join(chunks))
                    if not isinstance(payload, dict) or not isinstance(payload.get('vulnerabilities'), list):
                        raise NvdError('NVD JSON 응답 형식 불일치')
                    return payload
            except (requests.RequestException, ValueError) as exc:
                if attempt == 2:
                    raise NvdError(f'NVD 응답/연결 실패: {type(exc).__name__}') from None
                time.sleep(8 * (attempt + 1))
        raise NvdError('NVD 요청 실패')


def cvss_metrics(metrics: dict) -> list[dict]:
    """Return provider scores as published. Never calculate/guess a missing score."""
    rows, seen = [], set()
    if not isinstance(metrics, dict):
        return rows
    for key, version in VERSIONS:
        values = metrics.get(key, [])
        if not isinstance(values, list):
            continue
        valid = []
        for value in values:
            if not isinstance(value, dict) or not isinstance(value.get('cvssData'), dict):
                continue
            data = value['cvssData']
            score = data.get('baseScore')
            if (isinstance(score, bool) or not isinstance(score, (int, float))
                    or not math.isfinite(score) or not 0 <= score <= 10):
                continue
            # Reject an inconsistent metric version rather than relabel it.
            if str(data.get('version', version)) != version:
                continue
            severity = str(data.get('baseSeverity') or value.get('baseSeverity') or 'UNKNOWN').upper()
            row = {'version': version, 'score': float(score),
                   'severity': severity if severity in SEVERITIES else 'UNKNOWN',
                   'source': str(value.get('source') or '출처 미제공')[:160],
                   'type': str(value.get('type') or '')[:24],
                   'vector': str(data.get('vectorString') or '')[:600]}
            marker = (row['version'], row['source'], row['score'], row['vector'])
            if marker not in seen:
                seen.add(marker)
                valid.append(row)
        # Newer CVSS version first, then Primary, then NVD within a type.
        # NOT the highest vendor score chosen to sensationalize severity.
        valid.sort(key=lambda x: (x['type'] != 'Primary', x['source'] != 'nvd@nist.gov', x['source'], x['vector']))
        rows.extend(valid)
    return rows


def product_evidence(configurations) -> tuple[list[str], list[dict]]:
    """Keep only explicitly vulnerable CPEs, never infer an affected product."""
    stack = list(configurations) if isinstance(configurations, list) else []
    cpes, products, seen = [], [], set()
    visited = 0
    while stack and visited < 2000 and len(cpes) < 100:
        node = stack.pop()
        visited += 1
        if not isinstance(node, dict):
            continue
        for field in ('nodes', 'children'):
            if isinstance(node.get(field), list):
                stack.extend(node[field])
        matches = node.get('cpeMatch', [])
        if not isinstance(matches, list):
            continue
        for match in matches:
            if not isinstance(match, dict) or match.get('vulnerable') is not True:
                continue
            criteria = match.get('criteria')
            if (not isinstance(criteria, str) or len(criteria) > 2000
                    or not criteria.startswith('cpe:2.3:') or criteria in seen):
                continue
            # Escaped colons are part of the vendor/product, not separators.
            parts = re.split(r'(?<!\\):', criteria)
            if len(parts) != 13 or parts[2] not in ('a', 'o', 'h'):
                continue
            seen.add(criteria)
            cpes.append(criteria)
            product = {'part': parts[2], 'vendor': parts[3], 'product': parts[4],
                       'version': parts[5], 'criteria': criteria}
            for field in ('versionStartIncluding', 'versionStartExcluding',
                          'versionEndIncluding', 'versionEndExcluding'):
                if isinstance(match.get(field), str):
                    product[field] = match[field][:160]
            products.append(product)
            if len(cpes) >= 100:
                break
    return cpes, products


def reference_evidence(references) -> list[dict]:
    rows, seen = [], set()
    for ref in references if isinstance(references, list) else []:
        if not isinstance(ref, dict) or not isinstance(ref.get('url'), str):
            continue
        url = ref['url'].strip()
        if len(url) > 2000 or url in seen:
            continue
        try:
            canonical_url(url)  # Validation only: retain the original evidence URL.
        except (ValueError, UnicodeError):
            continue
        seen.add(url)
        tags = ref.get('tags', [])
        rows.append({'url': url, 'source': str(ref.get('source') or '')[:160],
                     'tags': [tag[:80] for tag in tags[:20] if isinstance(tag, str)]
                         if isinstance(tags, list) else []})
        if len(rows) >= 40:
            break
    return rows


def kev_evidence(cve: dict) -> dict | None:
    """An absent CISA KEV date means unknown, not proof of no exploitation."""
    added = cve.get('cisaExploitAdd')
    if not isinstance(added, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', added):
        return None
    try:
        datetime.fromisoformat(added)
    except ValueError:
        return None
    return {'added_at': added,
            'required_action': str(cve.get('cisaRequiredAction') or '')[:1500],
            'vulnerability_name': str(cve.get('cisaVulnerabilityName') or '')[:500]}


def weakness_evidence(weaknesses) -> list[str]:
    found = set()
    for weakness in weaknesses if isinstance(weaknesses, list) else []:
        if not isinstance(weakness, dict):
            continue
        descriptions = weakness.get('description', [])
        for entry in descriptions if isinstance(descriptions, list) else []:
            if (isinstance(entry, dict) and isinstance(entry.get('value'), str)
                    and re.fullmatch(r'CWE-\d+', entry['value'])):
                found.add(entry['value'])
    return sorted(found)[:40]


def normalize_cve(cve: dict, now: datetime) -> dict:
    ident = str(cve.get('id', '')).upper()
    if not CVE_ID.fullmatch(ident):
        raise ValueError('잘못된 CVE ID')
    published = parse_date(cve.get('published'))  # timezone-less NVD datetimes are UTC
    if not published:
        raise ValueError('NVD 공개 시각 누락')
    descriptions = [x for x in cve.get('descriptions', []) if isinstance(x, dict) and x.get('value')]
    description = next((x for x in descriptions if x.get('lang') == 'en'), descriptions[0] if descriptions else {})
    text = re.sub(r'\s+', ' ', str(description.get('value', ''))).strip()
    metrics = cvss_metrics(cve.get('metrics', {}))
    cpes, products = product_evidence(cve.get('configurations'))
    weaknesses = weakness_evidence(cve.get('weaknesses'))
    kev = kev_evidence(cve)
    return {'id': ident, 'url': 'https://nvd.nist.gov/vuln/detail/' + ident,
            'published_at': published.isoformat(), 'published_day': published.date().isoformat(),
            'last_modified_at': (parse_date(cve.get('lastModified')) or published).isoformat(),
            'vuln_status': str(cve.get('vulnStatus', ''))[:80],
            'rejected': str(cve.get('vulnStatus', '')).lower() in ('reject', 'rejected'),
            'description': text[:6000], 'description_language': str(description.get('lang', 'en')),
            'description_hash': hashlib.sha256(text.encode()).hexdigest(),
            'cvss': metrics[0] if metrics else None, 'cvss_all': metrics,
            'cpes': cpes, 'products': products, 'weaknesses': weaknesses,
            'references': reference_evidence(cve.get('references')),
            'known_exploited': True if kev else None, 'kev': kev,
            'observed_at': now.astimezone(KST).isoformat(), 'summary_ko': '', 'summary_model': ''}


def collect_cves(now: datetime, cfg: dict, client=None):
    """Backfill all retained days; pagination failures preserve the successful prefix."""
    start, end = query_window(now, cfg['keep_days'])
    status = {'name': 'NVD · 일별 CVE', 'status': 'ok', 'count': 0, 'total': 0,
              'message': '', 'at': end.isoformat(), 'start': start.isoformat(),
              'end': end.isoformat(), 'pages': 0, 'invalid_count': 0}
    rows = {}
    index, expected_total = 0, None
    max_pages = int(cfg.get('nvd_max_pages_per_run', 20))
    client = client or NvdClient(cfg)
    try:
        for page_number in range(max_pages):
            print(f'[CVE 수집] NVD 페이지 {page_number + 1} · offset {index}', flush=True)
            payload = client.get_page({'pubStartDate': utc_parameter(start), 'pubEndDate': utc_parameter(end),
                                       'resultsPerPage': 2000, 'startIndex': index})
            entries = payload['vulnerabilities']
            if not isinstance(entries, list):
                raise NvdError('NVD CVE 목록 형식 불일치')
            total, returned_index = payload.get('totalResults'), payload.get('startIndex')
            if (type(total) is not int or total < 0 or type(returned_index) is not int
                    or returned_index != index):
                raise NvdError('NVD 페이지 번호/전체 개수 응답 불일치')
            if expected_total is not None and expected_total != total:
                status['status'] = 'partial'
                status['message'] = '조회 중 전체 개수가 변경되어 다음 실행에서 재확인합니다.'
            expected_total = total
            status['total'], status['pages'] = total, page_number + 1
            for entry in entries:
                try:
                    row = normalize_cve(entry['cve'], end)
                except (KeyError, TypeError, ValueError, AttributeError):
                    status['invalid_count'] += 1
                    continue
                when = parse_date(row['published_at'])
                if start <= when <= end:
                    rows[row['id']] = row
            index += len(entries)
            if index >= total:
                break
            if not entries:
                raise NvdError('NVD 전체 개수보다 일찍 빈 페이지 반환')
        else:
            raise NvdError('NVD 페이지 안전 상한 도달: 이번 결과는 일부이며 다음 실행에서 재조회합니다')
    except (NvdError, requests.RequestException, KeyError, TypeError, ValueError) as exc:
        status['status'] = 'partial' if rows else 'error'
        status['message'] = str(exc) if isinstance(exc, NvdError) else f'NVD 응답 오류: {type(exc).__name__}'
    if status['invalid_count']:
        status['status'] = 'partial'
        status['message'] += f' · 형식이 잘못된 {status["invalid_count"]}건 제외'
    status['count'] = len([r for r in rows.values() if not r['rejected']])
    status['message'] = (f"보관 기간 내 {status['count']}건 · NVD 공개일(KST) 기준 · " +
                         (status['message'] or '전체 페이지 조회 완료'))
    print(f"[CVE 수집 {status['status']}] {status['message']}", flush=True)
    return list(rows.values()), status


def merge_cves(state: dict, today: dict, rows: list[dict], status: dict,
               now: datetime, make_day: Callable[[str], dict]) -> None:
    """Refresh the private pool and canonical facts of already selected cards."""
    current_day = now.astimezone(KST).date().isoformat()
    cutoff = min([current_day, *state['days'].keys()])
    # Caller already prunes; derive fetch cutoff from status when available.
    if status.get('start'):
        cutoff = parse_date(status['start']).date().isoformat()
    existing = {**state['days'], current_day: today}
    for target in existing.values():
        candidate_pool(target)
    for row in rows:
        if row['rejected']:
            for day in existing.values():
                day.get('cve_candidates', {}).pop(row['id'], None)
                day.get('cves', {}).pop(row['id'], None)
            continue
        daykey = row['published_day']
        if not cutoff <= daykey <= current_day:
            continue
        for key, day in existing.items():
            if key != daykey:
                day.get('cve_candidates', {}).pop(row['id'], None)
                day.get('cves', {}).pop(row['id'], None)
        target = existing.setdefault(daykey, make_day(daykey))
        pool = candidate_pool(target)
        old = pool.get(row['id'], {})
        canonical = deepcopy(row)
        if old.get('description_hash') == row['description_hash']:
            for field in ('summary_ko', 'summary_model'):
                canonical[field] = old.get(field, '')
        pool[row['id']] = canonical
        if daykey != current_day:
            state['days'][daykey] = target
    # Failed refreshes keep prior cards and their last successful observation time.
    for daykey, target in existing.items():
        if not cutoff <= daykey <= current_day:
            continue
        refresh_selected_cves(target)
        previous = target.get('cve_meta', {})
        target['cve_meta'] = {**previous, 'status': status['status'], 'at': now.isoformat(),
                              'last_success_at': now.isoformat() if status['status'] == 'ok'
                                  else previous.get('last_success_at'),
                              'message': status['message'], 'count': len(target['cves']),
                              'candidate_count': len(target['cve_candidates'])}
        if target['cve_candidates'] and daykey != current_day:
            target['updated_at'] = now.isoformat()


def candidate_pool(day: dict) -> dict:
    """Migrate legacy public CVEs without losing original evidence or summaries."""
    pool = day.setdefault('cve_candidates', {})
    for ident, row in day.setdefault('cves', {}).items():
        if ident not in pool and not row.get('rejected'):
            canonical = deepcopy(row)
            for field in CURATION_FIELDS:
                canonical.pop(field, None)
            pool[ident] = canonical
    return pool


def original_signature(row: dict) -> str:
    data = {key: row.get(key) for key in
            ('description_hash', 'cvss_all', 'cvss', 'cpes', 'products',
             'weaknesses', 'references', 'known_exploited', 'kev', 'vuln_status')}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def related_cve(row: dict) -> dict:
    return {**{key: deepcopy(row.get(key)) for key in
               ('id', 'url', 'published_at', 'cvss', 'cvss_all', 'references',
                'known_exploited', 'kev', 'products')},
            'cvss_score': (row.get('cvss') or {}).get('score')}


def refresh_selected_cves(day: dict) -> None:
    """Drop rejected IDs and update originals, keeping Gemini judgments separate."""
    pool = candidate_pool(day)
    refreshed = {}
    for ident, previous in day.get('cves', {}).items():
        canonical = pool.get(ident)
        if not canonical or canonical.get('rejected'):
            continue
        row = deepcopy(canonical)
        curation = deepcopy(previous.get('curation', {}))
        previous_related = previous.get('related_ids', curation.get('related_ids', []))
        related = [rid for rid in previous_related if rid in pool and rid != ident
                   and not pool[rid].get('rejected')]
        old_related = {item['id']: item for item in previous.get('related_cves', [])}
        if curation and (original_signature(previous) != original_signature(canonical)
                or related != previous_related
                or any(old_related.get(rid) != related_cve(pool[rid]) for rid in related)):
            curation['stale'] = True
        if curation:
            curation['related_ids'] = related
            row['curation'] = curation
        row['related_ids'] = related
        row['related_cves'] = [related_cve(pool[rid]) for rid in related]
        refreshed[ident] = row
    day['cves'] = refreshed


def apply_cve_selection(day: dict, picks: list[dict], now: datetime, model: str) -> None:
    """Commit a fully validated maximum of twenty Gemini-selected representatives."""
    pool = candidate_pool(day)
    if not isinstance(picks, list) or len(picks) > 20:
        raise GeminiResponseValidationError('CVE 대표 선정은 최대 20건입니다')
    selected, used = {}, set()
    try:
        for rank, pick in enumerate(picks, 1):
            ident = pick['id']
            related = pick['related_ids']
            scores, reason = pick['scores'], pick['reason_ko'].strip()
            if (not isinstance(ident, str) or not CVE_ID.fullmatch(ident)
                    or ident not in pool or pool[ident].get('rejected')
                    or pool[ident].get('id') != ident
                    or ident in used):
                raise GeminiResponseValidationError('CVE 대표 선정 ID가 유효하지 않습니다')
            if (not isinstance(related, list) or len(related) > max(0, len(pool) - 1)
                    or any(not isinstance(rid, str) or rid not in pool or rid == ident
                           or pool[rid].get('id') != rid or pool[rid].get('rejected')
                           for rid in related)
                    or len(set(related)) != len(related) or set(related) & used):
                raise GeminiResponseValidationError('CVE 관련 ID가 유효하지 않습니다')
            if (not isinstance(scores, dict) or set(scores) != CURATION_SCORE_KEYS
                    or any(isinstance(score, bool) or not isinstance(score, int)
                           or not 0 <= score <= 5
                           for score in scores.values())
                    or not reason or len(reason) > 500 or not re.search(r'[가-힣]', reason)):
                raise GeminiResponseValidationError('CVE 중요도 또는 선정 이유가 유효하지 않습니다')
            used.update([ident, *related])
            row = deepcopy(pool[ident])
            row['curation'] = {'scores': deepcopy(scores), 'reason_ko': reason,
                               'rank': rank, 'model': model, 'at': now.isoformat(),
                               'related_ids': list(related)}
            row['related_ids'] = list(related)
            row['related_cves'] = [related_cve(pool[rid]) for rid in related]
            selected[ident] = row
    except (KeyError, TypeError, AttributeError):
        raise GeminiResponseValidationError('CVE 대표 선정 응답 형식 불일치') from None
    day['cves'] = selected
    day.setdefault('cve_meta', {}).update(count=len(selected), candidate_count=len(pool))


def translate_cves(state: dict, today: dict, now: datetime, cfg: dict, client, checkpoint: Callable):
    """Translate today's selected originals; never process the private/archive backlog."""
    pending = [row for row in today.get('cves', {}).values()
               if not row.get('summary_ko') and row.get('description')]
    pending.sort(key=lambda row: (row.get('curation', {}).get('rank', 999),
                                 row['published_at']))
    batch_size = max(1, min(20, int(cfg.get('cve_summary_batch_size', 12))))
    max_calls = max(0, int(cfg.get('cve_summary_max_calls_per_run', 12)))
    starting_calls = client.calls
    translated, error = 0, ''
    for offset in range(0, len(pending), batch_size):
        # request() can retry up to three times; reserve all three before starting.
        if client.remaining < 3 or client.calls - starting_calls + 3 > max_calls:
            break
        batch = pending[offset:offset + batch_size]
        expected_ids = {row['id'] for row in batch}
        try:
            print(f'[CVE 요약] {len(batch)}건 · 완료 {translated}/{len(pending)}', flush=True)
            response = client.request(
                '각 CVE 설명을 한국어 1~2문장, 240자 이내로 요약하라. 대상 제품·취약점·영향은 '
                '제공된 설명에 있는 사실만 쓰고, 없는 버전·패치·공격 발생·CVSS 점수는 만들지 마라. '
                '공격 실행 절차나 페이로드를 추가하지 마라. 입력마다 id와 summary_ko를 반환하라. '
                'CVE ID를 그대로 복사하고 어떤 항목도 생략하지 마라.',
                {'cves': [{'id': row['id'], 'description': row['description'][:3000]} for row in batch]},
                summary_schema(expected_ids), client.summary_model,
                validator=lambda result: validate_summaries(result, expected_ids))
            # Injected/offline clients may not implement request's validation hook.
            validate_summaries(response, expected_ids)
            answers = response['cves']
            summaries = {a['id']: a['summary_ko'].strip() for a in answers}
            for row in batch:
                row['summary_ko'], row['summary_model'] = summaries[row['id']], client.summary_model
                canonical = today.get('cve_candidates', {}).get(row['id'])
                if canonical and canonical.get('description_hash') == row.get('description_hash'):
                    canonical['summary_ko'], canonical['summary_model'] = row['summary_ko'], row['summary_model']
                translated += 1
            checkpoint()
        except GeminiAuthenticationError:
            raise
        except (GeminiError, KeyError, TypeError, ValueError) as exc:
            error = str(exc) if isinstance(exc, GeminiError) else f'CVE 요약 응답 오류: {type(exc).__name__}'
            break
    total_pending = sum(not row.get('summary_ko') for row in today.get('cves', {}).values())
    return {'translated': translated, 'pending': total_pending, 'error': error}
