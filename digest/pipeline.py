from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from .common import (ROOT, CRON_SLOTS, read_json, write_json, retention_cutoff,
                     text_key, parse_date)
from .gemini import Gemini, GeminiError, GeminiAuthenticationError, HOT_CHUNK_SIZE, PROMPT_VERSION
from .network import PublicWeb, FetchError
from .sources import collect_sources, collect_github, prepare_article, in_window
from .cves import collect_cves, merge_cves, translate_cves


def empty_state() -> dict:
    return {'version': 1, 'days': {}, 'seen': {}, 'pending': {}, 'repo_cache': {}, 'last_run': {}}


def load_state(directory: Path) -> dict:
    state = read_json(directory / 'state.json', empty_state())
    if state.get('version') != 1 or any(not isinstance(state.get(k), dict) for k in empty_state() if k != 'version'):
        raise ValueError('지원하지 않거나 손상된 상태 파일입니다. 기존 news-state 브랜치를 보존하세요.')
    return state


def prune(state: dict, now: datetime, keep: int) -> None:
    cutoff = retention_cutoff(now, keep)
    state['days'] = {day: x for day, x in state['days'].items() if cutoff <= day <= now.date().isoformat()}
    state['seen'] = {key: x for key, x in state['seen'].items() if x['day'] >= cutoff}
    state['pending'] = {key: x for key, x in state['pending'].items() if x['first_seen_at'][:10] >= cutoff}
    state['repo_cache'] = {key: x for key, x in state['repo_cache'].items() if x['day'] >= cutoff}


def new_day(day: str) -> dict:
    return {'date': day, 'articles': {}, 'hot': [], 'hot_status': 'unavailable', 'hot_at': None,
            'hot_shortfall': '', 'slots': {}, 'manual_runs': 0, 'updated_at': None,
            'sources': [], 'warnings': [], 'pending_count': 0, 'cves': {}, 'cve_meta': {}}


def fair_queue(pending: dict) -> list[dict]:
    # Round-robin avoids an abundant arXiv feed starving Korean news.
    groups = defaultdict(deque)
    for x in sorted(pending.values(), key=lambda x: x['first_seen_at']):
        groups[x['source_id']].append(x)
    result = []
    while groups:
        for key in list(groups):
            result.append(groups[key].popleft())
            if not groups[key]:
                del groups[key]
    return result


def hot_round_calls(candidate_count: int) -> int:
    calls = 1
    while candidate_count > HOT_CHUNK_SIZE:
        groups = (candidate_count + HOT_CHUNK_SIZE - 1) // HOT_CHUNK_SIZE
        calls += groups
        candidate_count = groups * 10
    return calls


def repo_cache_key(item: dict, model: str) -> str:
    return sha256((PROMPT_VERSION + model + item['url'] + item['excerpt']).encode()).hexdigest()


def public_article(item: dict, summary: dict, now: datetime, model: str) -> dict:
    # No full original body or raw Gemini response is published/saved here.
    keys = ('id', 'url', 'source', 'source_id', 'region', 'kind', 'published_at', 'evidence_kind',
            'repo_name', 'stars_today', 'total_stars', 'programming_language', 'observed_at')
    output = {k: item.get(k) for k in keys}
    output.update({k: summary[k] for k in ('category', 'language', 'title_ko', 'summary_ko')})
    if item['kind'] == 'github':
        output['category'] = 'github'
    elif item['kind'] == 'event':
        output['category'] = 'event'
    elif item['kind'] == 'paper':
        output['category'] = 'tech'
    elif output['category'] == 'github':
        output['category'] = item['category_hint']
    output.update({'collected_at': now.isoformat(), 'summary_model': model,
                   'translation': summary['language'].lower() not in ('ko', 'kor', 'korean'),
                   'body_note': item.get('body_note', '')})
    return output


def run_pipeline(state: dict, directory: Path, now: datetime, cfg: dict, schedule: str = '',
                 web=None, gemini=None, source_loader=None, github_loader=None, cve_loader=None) -> dict:
    prune(state, now, cfg['keep_days'])
    daykey = now.date().isoformat()
    day = state['days'].get(daykey, new_day(daykey))
    day['warnings'] = []
    cve_status = None
    cve_summary = {'translated': 0, 'pending': 0, 'error': ''}
    cve_enabled = cfg.get('cve_enabled', False)
    client = gemini or Gemini(cfg)
    fetcher = web or PublicWeb(cfg['user_agent'], cfg['http_timeout_seconds'], cfg['per_host_delay_seconds'])
    sources = read_json(ROOT / 'config/sources.json', [])
    by_source = {x['id']: x for x in sources}
    articles, statuses = (source_loader or collect_sources)(fetcher, sources, now, cfg)
    repos, repo_status = (github_loader or collect_github)(fetcher, now, cfg)
    day['sources'] = statuses + [repo_status]
    # Leave enough calls to rank all candidates in bounded Gemini groups,
    # including possible alternate-model retries and the separate CVE budget.
    possible_candidates = len(day['articles']) + len(state['pending']) + len(articles) + len(repos)
    hot_calls = max(4, hot_round_calls(possible_candidates) * 3)
    cve_calls = max(0, int(cfg.get('cve_summary_max_calls_per_run', 12))) if cve_enabled else 0
    reserved_calls = min(cfg['max_api_calls_per_run'], hot_calls + cve_calls)
    if cve_enabled:
        cve_rows, cve_status = (cve_loader or collect_cves)(now, cfg)
        merge_cves(state, day, cve_rows, cve_status, now, new_day)
        day['sources'].append(cve_status)
    known_titles = {v.get('title_key') for v in state['seen'].values()}
    for item in articles:
        if item['id'] in state['seen']:
            state['pending'].pop(item['id'], None)
            continue
        title_hash = text_key(item['title_original'])
        if len(title_hash) >= 15 and title_hash in known_titles:
            state['seen'][item['id']] = {'day': daykey, 'title_key': title_hash}
            state['pending'].pop(item['id'], None)
            continue
        state['pending'].setdefault(item['id'], item)
    new_count = 0
    for item in repos:
        # Refresh the measured delta even when the description summary is cached.
        cached = state['repo_cache'].get(repo_cache_key(item, client.summary_model))
        if cached:
            cached['day'] = daykey
            if cached['summary']['relevant']:
                new_count += int(item['id'] not in day['articles'])
                day['articles'][item['id']] = public_article(item, cached['summary'], now, client.summary_model)
            state['pending'].pop(item['id'], None)
        else:
            state['pending'][item['id']] = item

    def checkpoint():
        if day['articles'] or day.get('cves') or day.get('cve_meta', {}).get('status') == 'ok':
            state['days'][daykey] = day
        write_json(directory / 'state.json', state)

    # Persist completed CVE data even if later news/API work is interrupted.
    checkpoint()

    def apply_batch(batch):
        nonlocal new_count
        print(f'[뉴스 요약] {len(batch)}건 · Gemini 요청 누적 {client.calls}회', flush=True)
        summaries = client.summarize(batch)
        for item in batch:
            summary = summaries[item['id']]
            if summary['relevant']:
                new_count += int(item['id'] not in day['articles'])
                day['articles'][item['id']] = public_article(item, summary, now, client.summary_model)
            if item['kind'] == 'github':
                state['repo_cache'][repo_cache_key(item, client.summary_model)] = {'day': daykey, 'summary': summary}
            else:
                state['seen'][item['id']] = {'day': daykey, 'title_key': text_key(item['title_original'])}
            state['pending'].pop(item['id'], None)
        checkpoint()

    batch, prepared_count = [], 0
    max_new = cfg['max_new_articles_per_run']
    limit_reached = False
    queued_titles = set()
    for item in fair_queue(state['pending']):
        if item['kind'] != 'github' and item['id'] in state['seen']:
            state['pending'].pop(item['id'], None)
            continue
        if item['kind'] == 'github' and item.get('observed_at', '')[:10] != daykey:
            state['pending'].pop(item['id'], None)
            continue
        if client.remaining <= reserved_calls or (max_new > 0 and prepared_count >= max_new):
            limit_reached = True
            break
        if item['kind'] != 'github' and not in_window(item, now, cfg['lookback_hours']):
            state['pending'].pop(item['id'], None)
            continue
        key = text_key(item['title_original'])
        if item['kind'] != 'github' and len(key) >= 15 and key in queued_titles:
            # Only drop exact normalized title duplicates after the first has succeeded.
            if any(x.get('title_key') == key for x in state['seen'].values()):
                state['seen'][item['id']] = {'day': daykey, 'title_key': key}
                state['pending'].pop(item['id'], None)
            continue
        try:
            if item['kind'] == 'github':
                if len(item['excerpt']) < 20:
                    raise FetchError('저장소 설명 부족: 제목으로 기능을 추측하지 않습니다')
                prepared = dict(item)
            else:
                source = by_source.get(item['source_id'])
                if not source or not source.get('enabled', True):
                    state['pending'].pop(item['id'], None)
                    continue
                prepared = prepare_article(fetcher, item, source, cfg)
                if not in_window(prepared, now, cfg['lookback_hours']):
                    state['pending'].pop(item['id'], None)
                    continue
            batch.append(prepared)
            prepared_count += 1
            queued_titles.add(key)
        except FetchError as exc:
            item['last_error'] = str(exc)
            continue
        if len(batch) >= cfg['batch_size']:
            try:
                apply_batch(batch)
                batch = []
            except GeminiAuthenticationError:
                raise
            except GeminiError as exc:
                day['warnings'].append(str(exc) + ' · 미완료 기사는 다음 실행에서 재시도합니다.')
                batch = []
                break
    if batch and client.remaining > reserved_calls - 1:
        try:
            apply_batch(batch)
        except GeminiAuthenticationError:
            raise
        except GeminiError as exc:
            day['warnings'].append(str(exc) + ' · 미완료 기사는 다음 실행에서 재시도합니다.')
    # Remove same-title pending copies only AFTER their first summary succeeded.
    completed_titles = {v.get('title_key') for v in state['seen'].values()}
    for ident, pending in list(state['pending'].items()):
        title = text_key(pending['title_original'])
        if pending['kind'] != 'github' and len(title) >= 15 and title in completed_titles:
            state['seen'][ident] = {'day': daykey, 'title_key': title}
            state['pending'].pop(ident, None)
    if limit_reached:
        day['warnings'].append('이번 실행의 처리 상한에 도달했습니다. 남은 기사는 다음 실행에서 처리합니다.')
    if day['articles']:
        try:
            print(f"[HOT 선정] 뉴스·논문·저장소 후보 {len(day['articles'])}건", flush=True)
            hot = client.select_hot(list(day['articles'].values()))
            day['hot'] = hot['picks']
            day['hot_model_used'] = hot.get('model_used', client.hot_model)
            day['hot_shortfall'] = hot['shortfall_reason_ko']
            day['hot_status'] = 'fresh'
            day['hot_at'] = now.isoformat()
        except GeminiAuthenticationError:
            raise
        except GeminiError as exc:
            # Never disguise a local ranking as a Gemini selection.
            day['hot_status'] = 'stale' if day['hot'] else 'unavailable'
            day['warnings'].append('HOT 재선정 실패: ' + str(exc))
    if cve_enabled:
        cve_summary = translate_cves(state, day, now, cfg, client, checkpoint)
        if cve_summary['error']:
            day['warnings'].append('CVE 요약 보류: ' + cve_summary['error'])
        if cve_summary['pending']:
            day['warnings'].append(f"보관 중 CVE {cve_summary['pending']}건은 한국어 요약 대기입니다. 점수와 원문 설명은 표시합니다.")
    failed_sources = sum(x['status'] != 'ok' for x in day['sources'])
    if failed_sources:
        day['warnings'].append(f'{failed_sources}개 수집원이 응답하지 않거나 수집을 제한했습니다. 수집 상태를 확인하세요.')
    day['pending_count'] = len(state['pending'])
    if day['pending_count']:
        day['warnings'].append(f'{day["pending_count"]}건은 근거 부족·호출 제한·요약 실패 등으로 보류 중입니다.')
    day['updated_at'] = now.isoformat()
    slot = CRON_SLOTS.get(schedule)
    if slot:
        day['slots'][slot] = {'at': now.isoformat(), 'status': 'partial' if day['warnings'] else 'ok',
                             'new_count': new_count}
    else:
        day['manual_runs'] += 1
    state['last_run'] = {'at': now.isoformat(), 'day': daykey, 'new_count': new_count,
                         'api_calls': client.calls, 'api_tokens': client.tokens,
                         'pending_count': len(state['pending']), 'sources': day['sources'],
                         'warnings': day['warnings'], 'slot': slot or '수동',
                         'summary_model': client.summary_model, 'hot_model': client.hot_model,
                         'hot_model_used': day.get('hot_model_used'),
                         'cve_count': len(day.get('cves', {})), 'cve_summary': cve_summary}
    checkpoint()
    return state['last_run']
