"""Publisher route, date extraction, warning and robots diagnostics tests."""
import json
import warnings
from copy import deepcopy
from datetime import datetime
from unittest.mock import Mock
from urllib.robotparser import RobotFileParser

import pytest
from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning
from defusedxml.common import DefusedXmlException

from digest.common import ROOT, KST, load_config
from digest.network import PublicWeb, FetchError
from digest.sources import (plain, parse_html_listing, parse_news_sitemap, collect_sources,
                            source_entries, page_publication_date, prepare_article)

NOW = datetime(2026, 9, 22, 13, 0, tzinfo=KST)
SOURCES = {x['id']: x for x in json.loads((ROOT/'config/sources.json').read_text())}
SITEMAP = b'''<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
<url><loc>https://thehackernews.com/2026/09/test-only.html</loc><news:news><news:publication_date>2026-09-22T01:00:00Z</news:publication_date><news:title>Test fixture security report</news:title></news:news></url></urlset>'''


def test_plain_filename_url_does_not_warn():
    with warnings.catch_warnings(record=True) as emitted:
        warnings.simplefilter('always')
        assert plain('example.html') == 'example.html'
        assert plain('https://example.invalid/file.txt') == 'https://example.invalid/file.txt'
        assert plain('AI &amp; security') == 'AI & security'
    assert not any(isinstance(w.message, MarkupResemblesLocatorWarning) for w in emitted)
    assert plain('<b>Test</b><script>evil()</script>') == 'Test'


def test_boan_new_selector_and_canonical_link():
    data = '<a href="/news/articleView.html?idxno=999991&utm_source=qa">보안뉴스 새 기사 경로를 확인하는 테스트</a>'.encode()
    source = SOURCES['boannews']
    row = parse_html_listing(data, source)[0]
    assert row['url'] == 'https://www.boannews.com/news/articleView.html?idxno=999991'
    with pytest.raises(FetchError): parse_html_listing(data, {**source,'link_selector':"a[href*='view.asp?idx=']"})


def test_thn_feed_failure_then_sitemap():
    web = Mock()
    web.get.side_effect = [FetchError('robots.txt 확인 실패: FeedBurner HTTP 403'),
                          (SITEMAP, 'text/xml', 'https://thehackernews.com/news-sitemap.xml')]
    rows, statuses = collect_sources(web, [SOURCES['hackernews']], NOW, load_config())
    assert len(rows) == 1 and statuses[0]['status'] == 'ok'
    assert '대체 경로' in statuses[0]['message'] and '403' in statuses[0]['message']
    assert rows[0]['excerpt'] == '' and rows[0]['evidence_kind'] == 'listing'
    assert rows[0]['published_at'] == '2026-09-22T10:00:00+09:00'


def test_thn_html_third_route_relative_links():
    html = b'<a href="/2026/09/example.html"><h2>Security test long article title</h2>Read more</a>'
    web = Mock(get=Mock(side_effect=[FetchError('robots failed'), FetchError('sitemap failed'),
                           (html, 'text/html', 'https://thehackernews.com/')]))
    rows, route, _, errors = source_entries(web, SOURCES['hackernews'])
    assert route == 'html' and len(errors) == 2
    assert rows[0]['title_original'] == 'Security test long article title'
    assert rows[0]['url'] == 'https://thehackernews.com/2026/09/example.html'


def test_all_routes_failed_is_not_success():
    web = Mock(get=Mock(side_effect=FetchError('robots denied')))
    rows, statuses = collect_sources(web, [SOURCES['hackernews']], NOW, load_config())
    assert rows == [] and statuses[0]['status'] == 'error'
    assert web.get.call_count == 3


def test_no_sitemap_external_entity_expansion():
    bad = b'<!DOCTYPE a [<!ENTITY ext SYSTEM "file:///etc/passwd">]><urlset><url>&ext;</url></urlset>'
    with pytest.raises(DefusedXmlException): parse_news_sitemap(bad, SOURCES['hackernews'])


def test_offdomain_sitemap_entry_filtered():
    data = SITEMAP.replace(b'thehackernews.com/2026/', b'evil.invalid/2026/')
    web = Mock(get=Mock(side_effect=[FetchError('feed down'), (data,'text/xml','https://thehackernews.com/news-sitemap.xml')]))
    rows, _ = collect_sources(web, [SOURCES['hackernews']], NOW, load_config())
    assert rows == []


def test_json_ld_publication_not_modified():
    soup = BeautifulSoup('<script type="application/ld+json">{"@graph":[{"dateModified":"2026-09-22T12:00:00+09:00","datePublished":"2026-09-20T09:00:00+09:00"}]}</script>', 'html.parser')
    assert page_publication_date(soup,'KR').date().isoformat() == '2026-09-20'


def test_boan_visible_input_date_kst():
    soup = BeautifulSoup('<div>입력 2026.09.22 08:32</div>', 'html.parser')
    assert page_publication_date(soup, 'KR').isoformat() == '2026-09-22T08:32:00+09:00'


def test_boan_new_body_selector():
    text = '<meta name="pub_date" content="2026-09-22 08:32"><div id="article-view-content-div">' + '기사 본문 테스트입니다. '*20 + '</div>'
    item = {'url':'https://www.boannews.com/news/articleView.html?idxno=99999','excerpt':'','kind':'article','published_at':None}
    web = Mock(get=Mock(return_value=(text.encode(), 'text/html', item['url'])))
    result = prepare_article(web, item, SOURCES['boannews'], load_config())
    assert result['evidence_kind'] == 'body_excerpt' and '기사 본문' in result['excerpt']
    assert result['published_at'] == '2026-09-22T08:32:00+09:00'


def test_no_title_only_summary_on_body_failure():
    web = Mock(get=Mock(side_effect=FetchError('본문 접근 제한')))
    item = {'url':'https://thehackernews.com/2026/09/test.html','excerpt':'','kind':'article','published_at':None}
    with pytest.raises(FetchError, match='요약 근거 부족'):
        prepare_article(web,item,SOURCES['hackernews'],load_config())


def test_policy_failure_and_actual_disallow_have_distinct_messages(monkeypatch):
    web = PublicWeb('TestBot')
    monkeypatch.setattr(web,'validate',lambda url, hosts:url)
    monkeypatch.setattr(web,'_policy',lambda *args:False)
    web.robot_errors['https://example.com'] = 'HTTP 503'
    with pytest.raises(FetchError,match='확인 실패.*503'):
        web.get('https://example.com/news',['example.com'])
    robot = RobotFileParser(); robot.parse(['User-agent: *','Disallow: /'])
    monkeypatch.setattr(web,'_policy',lambda *args:robot)
    with pytest.raises(FetchError,match='규칙에 따른 수집 금지'):
        web.get('https://example.com/news',['example.com'])


def test_html_robots_response_not_accepted():
    web = PublicWeb('TestBot')
    web._get = Mock(return_value=(b'<html>access error</html>','text/html','https://example.com/robots.txt'))
    assert web._policy('https://example.com/news',['example.com']) is False
    assert 'HTML' in web.robot_errors['https://example.com']
