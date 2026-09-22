from __future__ import annotations

import re
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from defusedxml import ElementTree as SafeET

from .common import KST, canonical_url, item_id, parse_date
from .network import FetchError, PublicWeb


def plain(html: str) -> str:
    soup = BeautifulSoup(html or '', 'html.parser')
    for tag in soup(['script', 'style', 'iframe', 'form', 'noscript']):
        tag.decompose()
    return re.sub(r'\s+', ' ', soup.get_text(' ', strip=True)).strip()


def local_name(tag: str) -> str:
    return tag.rsplit('}', 1)[-1].lower()


def child_text(node, *names: str) -> str:
    for name in names:
        for child in node:
            if local_name(child.tag) == name.lower():
                return ''.join(child.itertext()).strip()
    return ''


def parse_feed(data: bytes, source: dict) -> list[dict]:
    """RSS 2, RSS/RDF 1 and Atom. Disable XML entity expansion."""
    # Python's XML parser does not accept every multibyte encoding directly.
    # Decode declared Korean encodings first; entity checks still use defusedxml.
    declaration = re.search(br"<\?xml[^>]*encoding=['\"]([^'\"]+)['\"]", data[:250], re.I)
    if declaration and declaration[1].lower() in (b'euc-kr', b'cp949', b'ks_c_5601-1987', b'windows-949'):
        decoded = data.decode('cp949')
        decoded = re.sub(r"(<\?xml[^>]*?)\s+encoding=['\"][^'\"]+['\"]", r'\1', decoded, count=1, flags=re.I)
        root = SafeET.fromstring(decoded)
    else:
        root = SafeET.fromstring(data)
    if local_name(root.tag) not in ('rss', 'rdf', 'feed'):
        raise FetchError('RSS/Atom이 아닌 응답: 주소 또는 사이트 정책 확인')
    items = []
    for node in root.iter():
        if local_name(node.tag) not in ('item', 'entry'):
            continue
        title = plain(child_text(node, 'title'))
        link = child_text(node, 'origlink', 'link')
        if not link:
            for element in node:
                if local_name(element.tag) == 'link' and element.get('rel', 'alternate') == 'alternate':
                    link = element.get('href', '')
                    break
        if not title or not link:
            continue
        try:
            link = canonical_url(urljoin(source['url'], link))
        except ValueError:
            continue
        excerpt = plain(child_text(node, 'encoded', 'content', 'description', 'summary'))
        date_text = child_text(node, 'pubdate', 'published', 'date', 'updated')
        published = parse_date(date_text, KST if source['region'] == 'KR' else None)
        items.append({'title_original': title, 'url': link, 'excerpt': excerpt,
                      'published_at': published.isoformat() if published else None})
    return items


def parse_html_listing(data: bytes, source: dict) -> list[dict]:
    soup = BeautifulSoup(data, 'html.parser')
    found = {}
    for a in soup.select(source['link_selector']):
        title = plain(a.get_text(' ', strip=True))
        if len(title) < 10:
            continue
        try:
            link = canonical_url(urljoin(source['url'], a.get('href', '')))
        except ValueError:
            continue
        key = item_id(link)
        if key not in found:
            found[key] = {'title_original': title, 'url': link, 'excerpt': '', 'published_at': None}
        if len(found) >= source.get('max_items', 30):
            break
    if not found:
        raise FetchError('기사 목록 선택자와 일치하는 링크 없음')
    return list(found.values())


def in_window(item: dict, now: datetime, hours: int) -> bool:
    published = parse_date(item.get('published_at'))
    return published is None or now - timedelta(hours=hours) <= published <= now + timedelta(minutes=15)


def collect_sources(web: PublicWeb, sources: list[dict], now: datetime, cfg: dict):
    collected, statuses = [], []
    for source in sources:
        if not source.get('enabled', True):
            continue
        try:
            data, _, _ = web.get(source['url'], source['hosts'])
            entries = (parse_feed(data, source) if source['type'] == 'rss'
                       else parse_html_listing(data, source))
            count = 0
            for item in entries:
                if not in_window(item, now, cfg['lookback_hours']):
                    continue
                item.update({'id': item_id(item['url']), 'source_id': source['id'],
                             'source': source['name'], 'region': source['region'],
                             'language_hint': source['language'], 'category_hint': source['category'],
                             'kind': 'paper' if source.get('paper') else 'article',
                             'evidence_kind': 'abstract' if source.get('paper') else 'rss',
                             'first_seen_at': now.isoformat()})
                item['excerpt'] = item['excerpt'][:cfg['max_input_chars_per_article']]
                collected.append(item)
                count += 1
            statuses.append({'name': source['name'], 'status': 'ok', 'count': count,
                             'message': f'최근 기사 {count}건 / 피드 항목 {len(entries)}건'})
        except Exception as exc:
            # A broken external source must not discard successful sources.
            message = str(exc) if isinstance(exc, FetchError) else type(exc).__name__
            statuses.append({'name': source['name'], 'status': 'error', 'count': 0, 'message': message})
    return collected, statuses


def prepare_article(web: PublicWeb, item: dict, source: dict, cfg: dict) -> dict:
    result = dict(item)
    body_error = ''
    if (cfg['fetch_article_body'] and source.get('fetch_body', True)
            and item['kind'] != 'paper'):
        try:
            data, content_type, _ = web.get(item['url'], source['hosts'])
            if 'html' not in content_type.lower():
                raise FetchError('HTML 본문이 아닌 응답')
            soup = BeautifulSoup(data, 'html.parser')
            if not result.get('published_at'):
                for selector in ('meta[property="article:published_time"]', 'meta[name="date"]',
                                 'meta[name="pubdate"]', 'time[datetime]'):
                    tag = soup.select_one(selector)
                    if tag:
                        dt = parse_date(tag.get('content') or tag.get('datetime'),
                                        KST if source['region'] == 'KR' else None)
                        if dt:
                            result['published_at'] = dt.isoformat()
                            break
            for tag in soup(['script', 'style', 'iframe', 'nav', 'aside', 'form', 'footer', 'header']):
                tag.decompose()
            selector = source.get('body_selector')
            area = soup.select_one(selector) if selector else None
            if area is None:
                area = soup.select_one('article') or soup.select_one('main')
            if area is None:
                raise FetchError('본문 선택자 불일치')
            text = plain(str(area))
            if len(text) >= 100:
                result['excerpt'] = text[:cfg['max_input_chars_per_article']]
                result['evidence_kind'] = 'body_excerpt'
            else:
                raise FetchError('본문 내용 부족')
        except Exception as exc:
            body_error = str(exc) if isinstance(exc, FetchError) else type(exc).__name__
    if len(result.get('excerpt', '')) < 60:
        raise FetchError('요약 근거 부족: ' + (body_error or 'RSS에 본문/설명 없음'))
    result['body_note'] = body_error
    return result


def parse_number(text: str) -> int:
    cleaned = text.strip().replace(',', '')
    match = re.search(r'([0-9]+(?:\.[0-9]+)?)([kKmM]?)', cleaned)
    if not match:
        return 0
    return int(float(match[1]) * {'': 1, 'k': 1000, 'm': 1000000}[match[2].lower()])


def parse_trending(data: bytes, now: datetime, limit: int = 25) -> list[dict]:
    soup = BeautifulSoup(data, 'html.parser')
    rows = []
    for box in soup.select('article.Box-row'):
        link = box.select_one('h2 a')
        if not link:
            continue
        path = link.get('href', '').strip('/')
        if not re.fullmatch(r'[\w.-]+/[\w.-]+', path):
            continue
        # Read GitHub's published daily delta, never substitute total stars.
        daily = re.search(r'([\d,]+)\s+stars?\s+today', box.get_text(' ', strip=True), re.I)
        if not daily:
            continue
        description = box.select_one('p')
        total = box.select_one('a[href$="/stargazers"]')
        language = box.select_one('[itemprop="programmingLanguage"]')
        url = 'https://github.com/' + path
        rows.append({'id': item_id(url), 'url': url, 'repo_name': path,
                     'title_original': path,
                     'excerpt': plain(str(description)) if description else '',
                     'source_id': 'github', 'source': 'GitHub Trending', 'region': 'GLOBAL',
                     'kind': 'github', 'category_hint': 'github', 'language_hint': 'en',
                     'evidence_kind': 'repository_description', 'first_seen_at': now.isoformat(),
                     'published_at': None, 'observed_at': now.isoformat(),
                     'stars_today': int(daily[1].replace(',', '')),
                     'total_stars': parse_number(total.get_text()) if total else None,
                     'programming_language': language.get_text(strip=True) if language else None})
    if not rows:
        raise FetchError('GitHub Trending의 일간 스타 지표를 읽지 못했습니다')
    return sorted(rows, key=lambda x: x['stars_today'], reverse=True)[:limit]


def collect_github(web: PublicWeb, now: datetime, cfg: dict):
    try:
        data, _, _ = web.get(cfg['github_trending_url'], ['github.com'])
        rows = parse_trending(data, now, cfg['github_max_items'])
        return rows, {'name': 'GitHub Trending', 'status': 'ok', 'count': len(rows), 'message': 'stars today 기준'}
    except Exception as exc:
        message = str(exc) if isinstance(exc, FetchError) else type(exc).__name__
        return [], {'name': 'GitHub Trending', 'status': 'error', 'count': 0, 'message': message}
