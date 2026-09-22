"""Daily CVEs from NVD's documented REST API, not scraped news or AI scores.

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
from datetime import datetime, timedelta, timezone
from typing import Callable

import requests

from .common import KST, parse_date
from .gemini import GeminiError

ENDPOINT = 'https://services.nvd.nist.gov/rest/json/cves/2.0'
CVE_ID = re.compile(r'CVE-\d{4}-\d{4,}')
VERSIONS = (('cvssMetricV40', '4.0'), ('cvssMetricV31', '3.1'),
            ('cvssMetricV30', '3.0'), ('cvssMetricV2', '2.0'))
SEVERITIES = {'NONE', 'LOW', 'MEDIUM', 'HIGH', 'CRITICAL', 'UNKNOWN'}
SUMMARY_SCHEMA = {
    'type': 'object', 'properties': {'cves': {'type': 'array', 'items': {
        'type': 'object', 'properties': {'id': {'type': 'string'}, 'summary_ko': {'type': 'string'}},
        'required': ['id', 'summary_ko']}}}, 'required': ['cves']}


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
    return {'id': ident, 'url': 'https://nvd.nist.gov/vuln/detail/' + ident,
            'published_at': published.isoformat(), 'published_day': published.date().isoformat(),
            'last_modified_at': (parse_date(cve.get('lastModified')) or published).isoformat(),
            'vuln_status': str(cve.get('vulnStatus', ''))[:80],
            'rejected': str(cve.get('vulnStatus', '')).lower() in ('reject', 'rejected'),
            'description': text[:6000], 'description_language': str(description.get('lang', 'en')),
            'description_hash': hashlib.sha256(text.encode()).hexdigest(),
            'cvss': metrics[0] if metrics else None, 'cvss_all': metrics,
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
    """Update by CVE ID and publication day without relabeling old CVEs as today's."""
    current_day = now.astimezone(KST).date().isoformat()
    cutoff = min([current_day, *state['days'].keys()])
    # Caller already prunes; derive fetch cutoff from status when available.
    if status.get('start'):
        cutoff = parse_date(status['start']).date().isoformat()
    existing = {**state['days'], current_day: today}
    for row in rows:
        daykey = row['published_day']
        if not cutoff <= daykey <= current_day:
            continue
        for key, day in existing.items():
            if row['rejected'] or key != daykey:
                day.get('cves', {}).pop(row['id'], None)
        if row['rejected']:
            continue
        target = existing.setdefault(daykey, make_day(daykey))
        old = target.setdefault('cves', {}).get(row['id'], {})
        if old.get('description_hash') == row['description_hash']:
            for field in ('summary_ko', 'summary_model'):
                row[field] = old.get(field, '')
        target['cves'][row['id']] = row
        if daykey != current_day:
            state['days'][daykey] = target
    # Failed refreshes keep prior cards and their last successful observation time.
    for daykey, target in existing.items():
        if not cutoff <= daykey <= current_day:
            continue
        target.setdefault('cves', {})
        previous = target.get('cve_meta', {})
        target['cve_meta'] = {'status': status['status'], 'at': now.isoformat(),
                              'last_success_at': now.isoformat() if status['status'] == 'ok'
                                  else previous.get('last_success_at'),
                              'message': status['message'], 'count': len(target['cves'])}
        if target['cves'] and daykey != current_day:
            target['updated_at'] = now.isoformat()


def translate_cves(state: dict, today: dict, now: datetime, cfg: dict, client, checkpoint: Callable):
    """Only translate descriptions. No AI relevance filtering or score generation."""
    by_day = {**state['days'], today['date']: today}
    pending = [(key, row) for key, day in by_day.items() for row in day.get('cves', {}).values()
               if not row.get('summary_ko') and row.get('description')]
    pending.sort(key=lambda x: (x[0] == today['date'], x[0],
                 x[1]['cvss']['score'] if x[1]['cvss'] else -1, x[1]['published_at']), reverse=True)
    batch_size = max(1, min(20, int(cfg.get('cve_summary_batch_size', 12))))
    max_calls = max(0, int(cfg.get('cve_summary_max_calls_per_run', 12)))
    starting_calls = client.calls
    translated, error = 0, ''
    for offset in range(0, len(pending), batch_size):
        # request() can retry up to three times; reserve all three before starting.
        if client.remaining < 3 or client.calls - starting_calls + 3 > max_calls:
            break
        batch = pending[offset:offset + batch_size]
        try:
            print(f'[CVE 요약] {len(batch)}건 · 완료 {translated}/{len(pending)}', flush=True)
            response = client.request(
                '각 CVE 설명을 한국어 1~2문장, 240자 이내로 요약하라. 대상 제품·취약점·영향은 '
                '제공된 설명에 있는 사실만 쓰고, 없는 버전·패치·공격 발생·CVSS 점수는 만들지 마라. '
                '공격 실행 절차나 페이로드를 추가하지 마라. 입력마다 id와 summary_ko를 반환하라. '
                'CVE ID를 그대로 복사하고 어떤 항목도 생략하지 마라.',
                {'cves': [{'id': row['id'], 'description': row['description'][:3000]} for _, row in batch]},
                SUMMARY_SCHEMA, client.summary_model)
            answers = response['cves']
            ids = [answer['id'] for answer in answers]
            if len(set(ids)) != len(ids) or set(ids) != {row['id'] for _, row in batch}:
                raise GeminiError('CVE 요약의 ID 누락·중복·변조')
            for answer in answers:
                text = answer['summary_ko'].strip()
                if not text or len(text) > 500 or not re.search(r'[가-힣]', text):
                    raise GeminiError('CVE 한국어 요약 형식 불일치')
            summaries = {a['id']: a['summary_ko'].strip() for a in answers}
            for _, row in batch:
                row['summary_ko'], row['summary_model'] = summaries[row['id']], client.summary_model
                translated += 1
            checkpoint()
        except (GeminiError, KeyError, TypeError, ValueError) as exc:
            error = str(exc) if isinstance(exc, GeminiError) else f'CVE 요약 응답 오류: {type(exc).__name__}'
            break
    total_pending = sum(not row.get('summary_ko') for day in by_day.values() for row in day.get('cves', {}).values())
    return {'translated': translated, 'pending': total_pending, 'error': error}
