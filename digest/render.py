from __future__ import annotations

import shutil
from copy import deepcopy
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .common import ROOT, CATEGORIES, SLOTS, parse_date, write_json


def format_time(value, pattern='%m.%d %H:%M'):
    dt = parse_date(value)
    return dt.strftime(pattern) if dt else '시각 미제공'


def render_context(day: dict, today: str) -> dict:
    out = deepcopy(day)
    articles = list(day['articles'].values())
    out['sections'] = {c: sorted([a for a in articles if a['category'] == c],
                       key=lambda a: a.get('published_at') or a['collected_at'], reverse=True)
                       for c in CATEGORIES}
    out['sections']['github'].sort(key=lambda a: a.get('stars_today') or 0, reverse=True)
    out['hot_cards'] = []
    for rank, pick in enumerate(day['hot'], 1):
        if pick['id'] in day['articles']:
            card = dict(day['articles'][pick['id']])
            card.update({'rank': rank, 'reason_ko': pick['reason_ko']})
            out['hot_cards'].append(card)
    out['count'] = len(articles)
    out['counts'] = {k: len(v) for k, v in out['sections'].items()}
    out['is_today'] = day['date'] == today
    date = datetime.fromisoformat(day['date'])
    out['date_label'] = date.strftime('%Y. %m. %d')
    out['weekday'] = '월화수목금토일'[date.weekday()] + '요일'
    out['completed_slots'] = len([v for v in day['slots'].values() if v['status'] == 'ok'])
    return out


def render_site(state: dict, output: Path, now: datetime, cfg: dict) -> None:
    output = output.resolve()
    # Only remove the generated report subdirectory, never arbitrary output parents.
    if output == ROOT or output == Path('/'):
        raise ValueError('출력 경로를 별도 디렉터리로 지정하세요')
    output.mkdir(parents=True, exist_ok=True)
    reports_dir = output / 'reports'
    if reports_dir.exists():
        shutil.rmtree(reports_dir)
    reports_dir.mkdir()
    shutil.copytree(ROOT / 'assets', output / 'assets', dirs_exist_ok=True)
    env = Environment(loader=FileSystemLoader(ROOT / 'templates'),
                      autoescape=select_autoescape(['html', 'xml']), trim_blocks=True, lstrip_blocks=True)
    env.filters['kst'] = format_time
    days = [render_context(day, now.date().isoformat()) for _, day in sorted(state['days'].items(), reverse=True)]
    latest = 'reports/' + days[0]['date'] + '.html' if days else None
    today_day = next((day for day in days if day['is_today']), None)
    today_url = 'reports/' + today_day['date'] + '.html' if today_day else None
    common = {'site_name': cfg['site_name'], 'categories': CATEGORIES, 'slots': SLOTS,
              'days': days, 'generated_at': now.isoformat(), 'keep_days': cfg['keep_days'],
              'last_run': state.get('last_run', {}), 'latest_url': latest, 'today_url': today_url}
    for day in days:
        html = env.get_template('report.html').render(**common, day=day, page_type='report',
                prefix='../', page_title=f'{day["date_label"]} 데일리 리포트')
        (reports_dir / (day['date'] + '.html')).write_text(html, encoding='utf-8')
    html = env.get_template('index.html').render(**common, page_type='archive', prefix='./',
                                               page_title='데일리 브리핑 목록')
    (output / 'index.html').write_text(html, encoding='utf-8')
    html = env.get_template('not-found.html').render(**common, page_type='not-found', prefix='./', page_title='보고서를 찾을 수 없습니다')
    (output / '404.html').write_text(html, encoding='utf-8')
    (output / '.nojekyll').write_text('', encoding='utf-8')
    # Minimal metadata only; no original article bodies or API secrets.
    write_json(output / 'reports.json', {'generated_at': now.isoformat(), 'keep_days': cfg['keep_days'],
               'reports': [{'date': d['date'], 'url': 'reports/' + d['date'] + '.html',
                            'count': d['count'], 'hot_count': len(d['hot_cards']), 'updated_at': d['updated_at']}
                           for d in days]})
