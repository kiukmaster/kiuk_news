from __future__ import annotations
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from bs4 import BeautifulSoup
from defusedxml.common import DefusedXmlException

from digest.common import (KST, canonical_url, item_id, parse_date, retention_cutoff, load_config, text_key)
from digest.gemini import Gemini, GeminiError, response_text
from digest.network import PublicWeb, FetchError
from digest.pipeline import empty_state, load_state, new_day, prune, run_pipeline, fair_queue
from digest.render import render_site
from digest.sources import parse_feed, parse_html_listing, parse_trending, parse_number, in_window

NOW = datetime(2026, 9, 22, 6, 5, tzinfo=KST)
SOURCE = {'id': 'aitimes', 'name': '테스트 수집원', 'url': 'https://example.com/rss', 'region': 'KR'}


def article(index: int, now=NOW) -> dict:
    url = f'https://example.com/news/{index}'
    return {'id': item_id(url), 'url': url, 'title_original': f'독립적인 테스트 기사 {index}의 보안 연구 결과',
            'excerpt': f'검증용 기사 {index}. ' + '이 내용은 자동화된 테스트용이며 실제 뉴스가 아닙니다. ' * 5,
            'published_at': now.isoformat(), 'first_seen_at': now.isoformat(), 'source_id': 'aitimes',
            'source': '테스트 수집원', 'region': 'KR', 'kind': 'article', 'language_hint': 'ko',
            'category_hint': 'ai', 'evidence_kind': 'rss'}


class FakeGemini:
    def __init__(self, fail_summary=False, fail_hot=False, budget=80):
        self.summary_model = self.hot_model = 'test-only-not-real-gemini'
        self.calls = self.tokens = 0
        self.summarized = []
        self.fail_summary, self.fail_hot, self.budget = fail_summary, fail_hot, budget

    @property
    def remaining(self):
        return self.budget - self.calls

    def summarize(self, items):
        self.calls += 1
        if self.fail_summary:
            raise GeminiError('테스트용 요약 실패')
        self.summarized.extend(x['id'] for x in items)
        return {x['id']: {'id': x['id'], 'relevant': True, 'language': 'ko', 'category': x['category_hint'],
                'title_ko': x['title_original'], 'summary_ko': '레이아웃과 누적 처리를 확인하는 테스트용 요약입니다. 실제 기사가 아닙니다.'}
                for x in items}

    def select_hot(self, items):
        self.calls += 1
        if self.fail_hot:
            raise GeminiError('테스트용 HOT 실패')
        return {'picks': [{'id': x['id'], 'reason_ko': '자동 테스트용 선정 결과입니다.'} for x in items[:10]],
                'shortfall_reason_ko': ''}


def run(state, tmp_path, items, now=NOW, schedule='', client=None):
    cfg = load_config()
    cfg['fetch_article_body'] = False
    client = client or FakeGemini()
    run_pipeline(state, tmp_path, now, cfg, schedule=schedule, web=object(), gemini=client,
        source_loader=lambda *a: (deepcopy(items), [{'name': '테스트', 'status': 'ok', 'count': len(items), 'message': '테스트'}]),
        github_loader=lambda *a: ([], {'name': 'GitHub', 'status': 'ok', 'count': 0, 'message': '테스트'}))
    return client


@pytest.mark.parametrize('url', ['javascript:alert(1)', 'file:///etc/passwd', 'https://a:b@example.com/', 'https://example.com:8080/'])
def test_unsafe_urls(url):
    with pytest.raises(ValueError): canonical_url(url)


def test_tracking_and_ids():
    url = canonical_url('https://EXAMPLE.com/news?utm_source=rss&idxno=512&code=7&fbclid=x#title')
    assert url == 'https://example.com/news?code=7&idxno=512'
    assert item_id('http://www.example.com/news/1/') == item_id('https://example.com/news/1')
    assert item_id('https://example.com/?idxno=1') != item_id('https://example.com/?idxno=2')


def test_kst_boundary():
    assert parse_date('2026-09-21T21:00:00Z').date().isoformat() == '2026-09-22'
    assert parse_date('Tue, 22 Sep 2026 04:00:00 GMT').hour == 13
    assert parse_date('2026-09-22T04:00:00', None).hour == 13
    assert parse_date('no date') is None
    assert retention_cutoff(NOW, 5) == '2026-09-18'


def test_rss():
    xml = b'''<?xml version="1.0"?><rss version="2.0"><channel><item><title>Test &amp; AI</title><link>https://example.com/news/1?utm_source=rss</link><description>&lt;p&gt;Body&lt;/p&gt;&lt;script&gt;bad()&lt;/script&gt;</description><pubDate>Mon, 21 Sep 2026 21:00:00 GMT</pubDate></item></channel></rss>'''
    rows = parse_feed(xml, SOURCE)
    assert rows[0]['title_original'] == 'Test & AI'
    assert rows[0]['excerpt'] == 'Body'
    assert rows[0]['published_at'].startswith('2026-09-22T06:00:00')


def test_atom():
    xml = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>AI</title><link rel="self" href="https://example.com/feed"/><link rel="alternate" href="https://example.com/post"/><summary>Valid content</summary><published>2026-09-21T23:00:00Z</published></entry></feed>'''
    assert parse_feed(xml, SOURCE)[0]['url'] == 'https://example.com/post'


def test_rdf():
    xml = b'''<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/"><item><title>Paper</title><link>https://arxiv.org/abs/2609.00001</link><description>Research abstract</description><dc:date>2026-09-21T23:00:00Z</dc:date></item></rdf:RDF>'''
    assert parse_feed(xml, SOURCE)[0]['excerpt'] == 'Research abstract'


def test_nonfeed_and_xxe():
    with pytest.raises(FetchError): parse_feed(b'<html><body>Challenge</body></html>', SOURCE)
    with pytest.raises(DefusedXmlException):
        parse_feed(b'<!DOCTYPE rss [<!ENTITY x SYSTEM "file:///etc/passwd">]><rss>&x;</rss>', SOURCE)


def test_html_listing():
    source = {**SOURCE, 'link_selector': 'a[href*="view.asp?idx="]', 'max_items': 5}
    rows = parse_html_listing('<a href="/view.asp?idx=8">새로운 연구 결과를 소개하는 기사</a>'.encode(), source)
    assert rows[0]['url'] == 'https://example.com/view.asp?idx=8'


def test_window():
    assert in_window(article(1), NOW, 48)
    assert not in_window(article(1, NOW - timedelta(days=4)), NOW, 48)
    assert not in_window(article(1, NOW + timedelta(days=1)), NOW, 48)


def test_trending_uses_daily_not_total():
    html = '''<article class="Box-row"><h2><a href="/owner/first">owner/first</a></h2><p>AI project</p><a href="/owner/first/stargazers">100,000</a><span>12 stars today</span></article><article class="Box-row"><h2><a href="/owner/second">owner/second</a></h2><p>Security project</p><a href="/owner/second/stargazers">1,000</a><span>300 stars today</span></article>'''
    rows = parse_trending(html.encode(), NOW)
    assert rows[0]['repo_name'] == 'owner/second'
    assert rows[0]['stars_today'] == 300
    assert rows[1]['total_stars'] == 100000
    assert parse_number('14.8k') == 14800


def test_trending_missing_delta_fails():
    with pytest.raises(FetchError): parse_trending(b'<article class="Box-row"><h2><a href="/x/y">x/y</a></h2><a href="/x/y/stargazers">10000</a></article>', NOW)


def test_private_network_blocked():
    with patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('127.0.0.1', 0))]):
        with pytest.raises(FetchError): PublicWeb.validate('https://example.com/', ['example.com'])
    with pytest.raises(FetchError): PublicWeb.validate('https://other.example/', ['example.com'])


def test_response_parser_only_model_output():
    response = {'status': 'completed', 'steps': [
        {'type': 'user_input', 'content': [{'type': 'text', 'text': 'untrusted'}]},
        {'type': 'model_output', 'content': [{'type': 'text', 'text': '{"ok": true}'}]}]}
    assert json.loads(response_text(response)) == {'ok': True}
    with pytest.raises(GeminiError): response_text({'status': 'incomplete', 'steps': []})


def test_three_runs_one_report(tmp_path):
    state = empty_state()
    initial = [article(i) for i in range(11)]
    first = run(state, tmp_path, initial, schedule='0 21 * * *')
    assert len(first.summarized) == 11
    second = run(state, tmp_path, initial + [article(12)], now=NOW.replace(hour=13), schedule='0 4 * * *')
    assert len(second.summarized) == 1
    third = run(state, tmp_path, initial + [article(12)], now=NOW.replace(hour=19), schedule='0 10 * * *')
    assert third.summarized == []
    assert len(state['days']) == 1
    day = state['days']['2026-09-22']
    assert len(day['articles']) == 12 and len(day['hot']) == 10
    assert set(day['slots']) == {'06:00', '13:00', '19:00'}
    assert len(load_state(tmp_path)['days']) == 1


def test_manual_not_fake_scheduled_completion(tmp_path):
    state = empty_state()
    run(state, tmp_path, [article(1)])
    day = state['days']['2026-09-22']
    assert day['manual_runs'] == 1 and day['slots'] == {}


def test_summary_failure_keeps_pending(tmp_path):
    state = empty_state()
    run(state, tmp_path, [article(1)], client=FakeGemini(fail_summary=True))
    assert len(state['pending']) == 1
    assert not state['seen'] and not state['days']
    assert state['last_run']['warnings']


def test_hot_failure_retains_prior_selection(tmp_path):
    state = empty_state()
    run(state, tmp_path, [article(1)])
    old_hot = deepcopy(state['days']['2026-09-22']['hot'])
    run(state, tmp_path, [article(2)], now=NOW.replace(hour=13), client=FakeGemini(fail_hot=True))
    day = state['days']['2026-09-22']
    assert day['hot_status'] == 'stale' and day['hot'] == old_hot
    assert len(day['articles']) == 2


def test_budget_queues_unfinished(tmp_path):
    state = empty_state()
    run(state, tmp_path, [article(1)], client=FakeGemini(budget=4))
    assert len(state['pending']) == 1 and not state['seen']


def test_missing_evidence_is_not_hallucinated(tmp_path):
    state = empty_state()
    missing = article(1)
    missing['excerpt'] = ''
    client = run(state, tmp_path, [missing])
    assert not client.summarized
    assert state['pending'][missing['id']]['last_error']


def test_retention(tmp_path):
    state = empty_state()
    for i in range(8):
        date = (NOW - timedelta(days=i)).date().isoformat()
        state['days'][date] = new_day(date)
        state['seen'][date] = {'day': date}
    prune(state, NOW, 5)
    assert len(state['days']) == 5
    assert min(state['days']) == '2026-09-18'
    assert len(state['seen']) == 5


def test_corrupt_state_is_not_reset(tmp_path):
    (tmp_path / 'state.json').write_text('{broken')
    with pytest.raises(json.JSONDecodeError): load_state(tmp_path)


def test_fairness():
    pending = {}
    for i in range(4):
        row = article(i)
        row['source_id'] = 'arxiv' if i < 3 else 'korean'
        pending[row['id']] = row
    assert [x['source_id'] for x in fair_queue(pending)][:2] == ['arxiv', 'korean']


def test_safe_html_and_old_output_removed(tmp_path):
    state = empty_state()
    run(state, tmp_path / 'data', [article(1)])
    day = state['days']['2026-09-22']
    row = next(iter(day['articles'].values()))
    row['title_ko'] = '<script>alert(1)</script>'
    row['summary_ko'] = '" onclick="alert(1)" <img src=x onerror=alert(1)>'
    out = tmp_path / 'site'
    (out / 'reports').mkdir(parents=True)
    (out / 'reports/2000-01-01.html').write_text('expired')
    render_site(state, out, NOW, load_config())
    assert not (out / 'reports/2000-01-01.html').exists()
    html = (out / 'reports/2026-09-22.html').read_text()
    assert '<script>alert(1)</script>' not in html
    assert '&lt;script&gt;' in html
    assert '<img src=x' not in html
    assert 'cdn.tailwindcss.com' not in html
    soup = BeautifulSoup(html, 'html.parser')
    assert len(soup.select('.issue-card')) == 2
    assert soup.select_one('meta[name="viewport"]')['content'].find('user-scalable=no') == -1
    for a in soup.select('a[target="_blank"]'):
        assert 'noopener' in a['rel'] and 'noreferrer' in a['rel']
    for filename in out.rglob('*.html'):
        page = BeautifulSoup(filename.read_text(), 'html.parser')
        for a in page.select('a[href]'):
            href = a['href'].split('#')[0]
            if href and not href.startswith(('http:', 'https:')) and href not in ('./', '../'):
                assert (filename.parent / href).exists(), (filename, href)


def test_no_samples_in_empty_site(tmp_path):
    render_site(empty_state(), tmp_path / 'public', NOW, load_config())
    html = (tmp_path / 'public/index.html').read_text()
    assert '아직 생성된 보고서가 없습니다' in html
    assert not list((tmp_path / 'public/reports').glob('*.html'))
    for value in ('2025년 5월 20일', 'OpenSSH 인증 우회 신규', 'gemma-mesh-runtime', '수동 승인을 거칩니다'):
        assert value not in html


def test_korean_euckr_rss():
    xml = '<?xml version="1.0" encoding="EUC-KR"?><rss><channel><item><title>보안 테스트 기사</title><link>https://example.com/</link><description>테스트 데이터</description></item></channel></rss>'.encode('euc-kr')
    assert parse_feed(xml, SOURCE)[0]['title_original'] == '보안 테스트 기사'


def test_same_title_different_urls_do_not_leak_through_pending(tmp_path):
    first, second = article(701), article(702)
    second['title_original'] = first['title_original']
    state = empty_state()
    client = run(state, tmp_path, [first, second])
    assert len(client.summarized) == 1
    assert len(state['days'][NOW.date().isoformat()]['articles']) == 1
    assert not state['pending']
    next_client = run(state, tmp_path, [first, second], now=NOW+timedelta(hours=7))
    assert not next_client.summarized
    assert len(state['days'][NOW.date().isoformat()]['articles']) == 1


def test_failed_duplicate_summary_not_silently_lost(tmp_path):
    first, second = article(703), article(704)
    second['title_original'] = first['title_original']
    state = empty_state()
    run(state, tmp_path, [first, second], client=FakeGemini(fail_summary=True))
    assert len(state['pending']) == 2
    assert not state['seen']
    run(state, tmp_path, [first, second], now=NOW+timedelta(hours=1))
    assert len(state['days'][NOW.date().isoformat()]['articles']) == 1
    assert not state['pending']
