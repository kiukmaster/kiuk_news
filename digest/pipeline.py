from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path

from .common import (ROOT, KST, CRON_SLOTS, CATEGORIES, read_json, write_json, retention_cutoff,
                     text_key, parse_date)
from .gemini import Gemini, GeminiError, GeminiAuthenticationError, PROMPT_VERSION
from .budget import hot_round_calls, plan_api_budget
from .network import PublicWeb, FetchError
from .sources import collect_sources, collect_github, prepare_article, in_window
from .cves import collect_cves, merge_cves, translate_cves, apply_cve_selection
from .curation import curate_candidates
from .publication import publication_target


def empty_state() -> dict:
    return {'version': 1, 'days': {}, 'seen': {}, 'pending': {}, 'repo_cache': {}, 'last_run': {}}


def load_state(directory: Path) -> dict:
    state = read_json(directory / 'state.json', empty_state())
    if state.get('version') != 1 or any(not isinstance(state.get(k), dict) for k in empty_state() if k != 'version'):
        raise ValueError('지원하지 않거나 손상된 상태 파일입니다. 기존 news-state 브랜치를 보존하세요.')
    repair_slot_dates(state)
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
            'sources': [], 'warnings': [], 'pending_count': 0, 'cves': {}, 'cve_meta': {},
            'news_candidates': {}, 'cve_candidates': {}}


def capped_articles(articles: dict, limit: int) -> bool:
    return all(sum(row.get('category') == category for row in articles.values()) <= limit
               for category in CATEGORIES) and all(row.get('category') in CATEGORIES for row in articles.values())


def curate_news(day: dict, pending: dict, client, now: datetime, model: str, limit: int) -> dict:
    """Commit validated representative IDs before any body fetch or summary."""
    pool = day.setdefault('news_candidates', {})
    pool.update({ident: {**pool.get(ident, {}), **row} for ident, row in day['articles'].items()})
    candidates = list(pool.values())
    selection = curate_candidates(client, candidates, kind='news', model=model, limit=limit,
                                  as_of=now.isoformat())
    picks = {pick['id']: pick for pick in selection['picks']}
    ranks = defaultdict(int)
    for pick in picks.values():
        ranks[pick['category']] += 1
        pick['rank'] = ranks[pick['category']]
    day['articles'] = {ident: row for ident, row in day['articles'].items() if ident in picks}
    for ident, row in day['articles'].items():
        row['category'] = picks[ident]['category']
        row['curation'] = {**picks[ident], 'model': model, 'at': now.isoformat()}
    day['news_curation'] = {'status': 'fresh', 'model': model, 'at': now.isoformat(),
                            'limit': limit, 'candidate_count': len(candidates),
                            'selected_count': len(picks), 'shortfall_reason_ko': selection['shortfall_reason_ko']}
    # Previously summarized, subsequently promoted candidates reuse their summary.
    for ident, pick in picks.items():
        row = pool[ident]
        if ident not in day['articles'] and row.get('summary_ko'):
            day['articles'][ident] = {**row, 'category': pick['category'],
                                     'curation': {**pick, 'model': model, 'at': now.isoformat()}}
            pending.pop(ident, None)
        elif ident not in day['articles']:
            pending.setdefault(ident, dict(row))
    # HOT must never link to a representative removed by daily curation.
    day['hot'] = [pick for pick in day['hot'] if pick['id'] in day['articles']]
    return picks


def repair_slot_dates(state: dict) -> None:
    """Move legacy overnight executions to their intended publication day."""
    moved = []
    for date, day in list(state['days'].items()):
        for slot, cycle in list(day.get('slots', {}).items()):
            if slot not in CRON_SLOTS.values():
                continue
            at = parse_date(cycle.get('at'))
            target = parse_date(cycle.get('publish_at'))
            if not target and at:
                target = at.replace(hour=int(slot[:2]), minute=0, second=0, microsecond=0)
                # External dispatch can start a full hour before its target.
                if at < target - timedelta(hours=1):
                    target -= timedelta(days=1)
                cycle['publish_at'] = target.isoformat()
            if target and target.date().isoformat() != date:
                day['slots'].pop(slot)
                moved.append((target.date().isoformat(), slot, cycle))
    # Remove all misplaced entries before resolving collisions at destinations.
    for date, slot, cycle in moved:
        destination = state['days'].setdefault(date, new_day(date))
        previous = destination['slots'].get(slot)
        if not previous or (cycle.get('at') or '') > (previous.get('at') or ''):
            destination['slots'][slot] = cycle


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
                 web=None, gemini=None, source_loader=None, github_loader=None, cve_loader=None,
                 publication_at: datetime | None = None) -> dict:
    repair_slot_dates(state)
    prune(state, now, cfg['keep_days'])
    slot = CRON_SLOTS.get(schedule)
    if slot:
        publication_at = publication_at or publication_target(now, 'schedule', schedule)
        if publication_at.tzinfo is None:
            raise ValueError('게시 목표 시각에는 시간대가 필요합니다')
        publication_at = publication_at.astimezone(KST)
        if publication_at.strftime('%H:%M') != slot:
            raise ValueError('게시 목표와 수집 슬롯이 일치하지 않습니다')
    daykey = now.date().isoformat()
    day = state['days'].get(daykey, new_day(daykey))
    day['warnings'] = []
    previous_article_ids = set(day['articles'])
    limit = min(20, max(1, int(cfg.get('category_daily_limit', 20))))
    curation_model = cfg.get('curation_model', 'gemini-3.8-flash')
    pool = day.setdefault('news_candidates', {})
    pool.update({ident: {**pool.get(ident, {}), **row} for ident, row in day['articles'].items()})
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
    cve_calls = max(0, int(cfg.get('cve_summary_max_calls_per_run', 12))) if cve_enabled else 0
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
                pool[item['id']] = {**item, **public_article(item, cached['summary'], now, client.summary_model)}
            state['pending'].pop(item['id'], None)
        else:
            state['pending'][item['id']] = item

    for ident, item in list(state['pending'].items()):
        if (item['kind'] == 'github' and item.get('observed_at', '')[:10] != daykey
                or item['kind'] != 'github' and not in_window(item, now, cfg['lookback_hours'])):
            state['pending'].pop(ident, None)
            continue
        # Candidate metadata is enough for curation. Full bodies are fetched only
        # for selected representatives below; all candidates are not summarized.
        pool[ident] = {**item, 'excerpt': str(item.get('excerpt', ''))[:1500]}

    # The existing exact-title guard also applies to cached candidates so an
    # already discarded copy cannot be resurrected by tomorrow's retry pool.
    candidate_titles = set()
    for ident, item in list(pool.items()):
        title = text_key(item.get('title_original') or item.get('title_ko') or '')
        if item['kind'] != 'github' and len(title) >= 15:
            if title in candidate_titles:
                pool.pop(ident)
                state['pending'].pop(ident, None)
                day['articles'].pop(ident, None)
                state['seen'][ident] = {'day': daykey, 'title_key': None, 'excluded_duplicate': True}
            else:
                candidate_titles.add(title)

    selected = {}
    try:
        print(f'[뉴스 선별] 후보 {len(pool)}건 · 분야별 하루 최대 {limit}건 · {curation_model}', flush=True)
        selected = curate_news(day, state['pending'], client, now, curation_model, limit)
        for ident, item in list(state['pending'].items()):
            if ident not in selected:
                if item['kind'] != 'github':
                    state['seen'][ident] = {'day': daykey, 'title_key': None, 'excluded_by_curation': True}
                state['pending'].pop(ident, None)
    except GeminiAuthenticationError:
        raise
    except GeminiError as exc:
        previous = day.get('news_curation', {})
        preserve = previous.get('status') in ('fresh', 'stale') and capped_articles(day['articles'], limit)
        if not preserve:
            day['articles'] = {}
            day['hot'] = []
        day['news_curation'] = {**previous, 'status': 'stale' if preserve else 'unavailable',
                                'model': curation_model, 'limit': limit, 'candidate_count': len(pool)}
        day['warnings'].append('뉴스 선별 보류: ' + str(exc))

    if cve_enabled:
        cve_pool = day.get('cve_candidates', {})
        try:
            print(f'[CVE 선별] 후보 {len(cve_pool)}건 · 하루 최대 {limit}건 · {curation_model}', flush=True)
            cve_selection = curate_candidates(client, list(cve_pool.values()), kind='cve',
                                               model=curation_model, limit=limit, as_of=now.isoformat())
            apply_cve_selection(day, cve_selection['picks'], now, curation_model)
            day['cve_curation'] = {'status': 'fresh', 'model': curation_model, 'at': now.isoformat(),
                                  'limit': limit, 'candidate_count': len(cve_pool),
                                  'selected_count': len(day['cves']),
                                  'shortfall_reason_ko': cve_selection['shortfall_reason_ko']}
        except GeminiAuthenticationError:
            raise
        except GeminiError as exc:
            previous = day.get('cve_curation', {})
            preserve = previous.get('status') in ('fresh', 'stale') and len(day['cves']) <= limit
            if not preserve:
                day['cves'] = {}
            day['cve_curation'] = {**previous, 'status': 'stale' if preserve else 'unavailable',
                                  'model': curation_model, 'limit': limit, 'candidate_count': len(cve_pool),
                                  'selected_count': len(day['cves'])}
            day['warnings'].append('CVE 선별 보류: ' + str(exc))

    # Reserve translation for at most the twenty public CVE representatives.
    pending_cves = sum(not row.get('summary_ko') for row in day.get('cves', {}).values())
    cve_batch = max(1, min(20, int(cfg.get('cve_summary_batch_size', 12))))
    cve_calls = min(cve_calls, ((pending_cves + cve_batch - 1) // cve_batch) * 3)

    # A queued article is not yet a HOT candidate. Only completed summaries and
    # the batches this run can actually afford may increase the ranking budget.
    selected_pending = {ident: item for ident, item in state['pending'].items() if ident in selected}
    budget = plan_api_budget(client.remaining, len(day['articles']), len(selected_pending),
                             cfg['batch_size'], cve_calls, cfg['max_new_articles_per_run'])
    reserved_calls = budget.reserved_calls
    print(f'[API 예산] 뉴스 {budget.news_calls}회 · HOT {budget.hot_calls}회 · '
          f'CVE {budget.cve_calls}회', flush=True)

    def checkpoint():
        if day['articles'] or day.get('cves') or day.get('cve_meta', {}).get('status') == 'ok' or pool:
            state['days'][daykey] = day
        write_json(directory / 'state.json', state)

    # Persist completed CVE data even if later news/API work is interrupted.
    checkpoint()
    if day['news_curation']['status'] == 'unavailable' and pool:
        # A green deployment with an empty report hid an API schema rejection.
        # Preserve candidates for retry and keep the last deployed report.
        raise GeminiError('뉴스 선별을 완료하지 못해 게시를 중단합니다. ' +
                          next(message for message in day['warnings'] if message.startswith('뉴스 선별 보류:')))

    def apply_batch(batch):
        nonlocal new_count
        print(f'[뉴스 요약] {len(batch)}건 · Gemini 요청 누적 {client.calls}회', flush=True)
        summaries = client.summarize(batch)
        for item in batch:
            summary = summaries[item['id']]
            if summary['relevant']:
                new_count += int(item['id'] not in day['articles'])
                card = public_article(item, summary, now, client.summary_model)
                card['category'] = selected[item['id']]['category']
                card['curation'] = {**selected[item['id']], 'model': curation_model, 'at': now.isoformat()}
                day['articles'][item['id']] = card
                pool[item['id']].update(card)
            else:
                pool.pop(item['id'], None)
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
    for item in fair_queue(selected_pending):
        if item['kind'] != 'github' and item['id'] in state['seen'] and item['id'] not in selected:
            state['pending'].pop(item['id'], None)
            continue
        if item['kind'] == 'github' and item.get('observed_at', '')[:10] != daykey:
            state['pending'].pop(item['id'], None)
            continue
        if client.remaining < reserved_calls + 3 or (max_new > 0 and prepared_count >= max_new):
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
    if batch and client.remaining >= reserved_calls + 3:
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
        cve_cfg = {**cfg, 'cve_summary_max_calls_per_run': budget.cve_calls}
        cve_summary = translate_cves(state, day, now, cve_cfg, client, checkpoint)
        if cve_summary['error']:
            day['warnings'].append('CVE 요약 보류: ' + cve_summary['error'])
        if cve_summary['pending']:
            day['warnings'].append(f"선정 CVE {cve_summary['pending']}건은 한국어 요약 대기입니다. 점수와 원문 설명은 표시합니다.")
    failed_sources = sum(x['status'] != 'ok' for x in day['sources'])
    if failed_sources:
        day['warnings'].append(f'{failed_sources}개 수집원이 응답하지 않거나 수집을 제한했습니다. 수집 상태를 확인하세요.')
    day['pending_count'] = len(state['pending'])
    if day['pending_count']:
        day['warnings'].append(f'{day["pending_count"]}건은 근거 부족·호출 제한·요약 실패 등으로 보류 중입니다.')
    day['updated_at'] = now.isoformat()
    day['news_curation']['selected_count'] = len(day['articles'])
    new_count = len(set(day['articles']) - previous_article_ids)
    if slot:
        slot_day = publication_at.date().isoformat()
        if retention_cutoff(now, cfg['keep_days']) <= slot_day <= daykey:
            scheduled_day = day if slot_day == daykey else state['days'].setdefault(slot_day, new_day(slot_day))
            scheduled_day['slots'][slot] = {
                'at': now.isoformat(), 'publish_at': publication_at.isoformat(),
                'status': 'partial' if day['warnings'] else 'ok', 'new_count': new_count}
    else:
        day['manual_runs'] += 1
    state['last_run'] = {'at': now.isoformat(), 'day': daykey, 'new_count': new_count,
                         'api_calls': client.calls, 'api_tokens': client.tokens,
                         'pending_count': len(state['pending']), 'sources': day['sources'],
                         'warnings': day['warnings'], 'slot': slot or '수동',
                         'slot_day': publication_at.date().isoformat() if slot else None,
                         'publish_at': publication_at.isoformat() if slot else None,
                         'summary_model': client.summary_model, 'hot_model': client.hot_model,
                         'curation_model': curation_model, 'news_curation': day['news_curation'],
                         'cve_curation': day.get('cve_curation', {}),
                         'hot_model_used': day.get('hot_model_used'),
                         'cve_count': len(day.get('cves', {})), 'cve_summary': cve_summary}
    checkpoint()
    return state['last_run']
