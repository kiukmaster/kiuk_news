"""Responsive UI checks with visibly labelled synthetic test data, never published.
Run: python tests/browser_check.py [--screenshots DIRECTORY]
"""
from __future__ import annotations
import argparse
import functools
import json
import shutil
import sys
import tempfile
import threading
from datetime import timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from digest.common import now_kst, load_config, SLOTS
from digest.pipeline import empty_state, new_day
from digest.render import render_site
from digest.cves import normalize_cve
from playwright.sync_api import sync_playwright

SIZES = [(320,800),(344,882),(360,900),(390,844),(412,915),(600,700),
         (690,829),(768,900),(840,980),(900,700),(1024,768),(1440,1000)]


def fixture_state(now):
    state = empty_state()
    for offset in range(5):
        dt = now - timedelta(days=offset)
        date = dt.date().isoformat()
        day = new_day(date)
        day['updated_at'] = day['hot_at'] = dt.isoformat()
        day['hot_status'] = 'fresh'
        day['slots'] = {slot: {'at': dt.isoformat(), 'status': 'ok', 'new_count': 4} for slot in SLOTS}
        day['sources'] = [{'name': 'UI 검증 데이터 · 실제 뉴스 아님', 'status':'ok', 'message':'오프라인 시험'}]
        for i in range(12):
            category = ['ai', 'security', 'tech', 'github'][i % 4]
            key = f'test-{date}-{i}'
            a = {'id':key, 'url':'https://example.invalid/ui-test', 'source':'UI 검증 · 실제 뉴스 아님',
                'source_id':'ui-test', 'region':'KR' if i%2==0 else 'GLOBAL',
                'kind':'github' if category=='github' else ('paper' if category=='tech' else 'article'),
                'published_at':None if category=='github' else dt.isoformat(), 'evidence_kind':'rss',
                'collected_at':dt.isoformat(), 'observed_at':dt.isoformat(),
                'category':category, 'language':'ko', 'translation':bool(i%2), 'body_note':'',
                'title_ko':f'[화면 검증 {i+1:02}] 폴드 화면에서 카드 제목과 한국어 요약의 배치를 확인합니다',
                'summary_ko':'실제 뉴스가 아닌 자동 테스트용 내용입니다. 좁은 커버 화면과 펼친 내부 화면에서 긴 제목, 요약, 원문 링크가 잘리는지 확인합니다.',
                'repo_name':'ui-test/'+'long-repository-name-'*5 if category=='github' else None,
                'stars_today':(i+1)*123, 'total_stars':(i+1)*9876, 'programming_language':'Python',
                'summary_model':'offline-ui-test'}
            day['articles'][key] = a
        day['hot'] = [{'id':key, 'reason_ko':'실제 선정이 아닌 화면 검증용 항목입니다.'} for key in list(day['articles'])[:10]]
        for cve_index in range(45 if offset == 0 else 3):
            raw_cve = {'id':f'CVE-2099-{90000+cve_index}', 'published':dt.isoformat(),
                       'lastModified':dt.isoformat(), 'vulnStatus':'SYNTHETIC UI TEST ONLY',
                       'descriptions':[{'lang':'en','value':'SYNTHETIC TEST DATA, NOT A REAL CVE. '+ 'Long description for a test layout. '*20}],
                       'metrics':{} if cve_index == 0 else {'cvssMetricV31':[
                         {'source':'synthetic-provider-'+'long-name-'*12+'@example.invalid','type':'Primary',
                          'cvssData':{'version':'3.1','baseScore':7.5,'baseSeverity':'HIGH',
                                      'vectorString':'CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:H'}}]}}
            row = normalize_cve(raw_cve, now)
            if cve_index % 2:
                row['summary_ko'] = '실제 CVE가 아닌 화면 검증용 데이터입니다. 점수 제공기관 이름과 설명이 긴 경우에도 좁은 화면에서 잘리지 않는지 확인합니다.'
            day['cves'][row['id']] = row
        day['cve_meta'] = {'status':'ok','at':now.isoformat(),'last_success_at':now.isoformat(),'message':'오프라인 시험 데이터'}
        state['days'][date] = day
    return state


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--screenshots', type=Path)
    parser.add_argument('--in-memory', action='store_true', help='제한된 환경의 레이아웃 시험: HTTP/CSP/실제 탐색은 시험하지 않음')
    args = parser.parse_args()
    screenshots = args.screenshots.resolve() if args.screenshots else None
    if screenshots: screenshots.mkdir(parents=True, exist_ok=True)
    now = now_kst()
    results = []
    errors = []
    with tempfile.TemporaryDirectory() as temp:
        temp = Path(temp)
        output = temp / 'project-prefix'
        render_site(fixture_state(now), output, now, load_config())
        handler = functools.partial(QuietHandler, directory=str(temp))
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}/project-prefix/'
        try:
            with sync_playwright() as p:
                executable = shutil.which('chromium') or shutil.which('chromium-browser')
                browser = p.chromium.launch(headless=True, executable_path=executable)
                context = browser.new_context(locale='ko-KR', timezone_id='Asia/Seoul', reduced_motion='reduce')
                page = context.new_page()
                page.set_default_timeout(5000)
                page.set_default_navigation_timeout(5000)
                page.on('pageerror', lambda error: errors.append(str(error)))
                def load(path, js=True, target=None):
                    target = target or page
                    if not args.in_memory:
                        response = target.goto(base + path, wait_until='networkidle')
                        assert response.status == 200
                        return
                    soup = BeautifulSoup((output / path).read_text(encoding='utf-8'), 'html.parser')
                    for element in soup.select('meta[http-equiv="Content-Security-Policy"], link, script'):
                        element.decompose()
                    target.set_content(str(soup), wait_until='load')
                    target.add_style_tag(content=(ROOT/'assets/style.css').read_text(encoding='utf-8'))
                    if js:
                        target.evaluate("""() => {
                          window.__testStorage = window.__testStorage || {};
                          Object.defineProperty(window, 'localStorage', {configurable: true, value: {
                            getItem: key => window.__testStorage[key] ?? null,
                            setItem: (key,value) => {window.__testStorage[key]=String(value);}
                          }});
                        }""")
                        target.add_script_tag(content=(ROOT/'assets/app.js').read_text(encoding='utf-8'))
                for width, height in SIZES:
                    page.set_viewport_size({'width':width,'height':height})
                    for kind, path, grid in [('archive','index.html','.card-grid'),
                            ('report',f'reports/{now.date().isoformat()}.html','#sec-hot .report-grid')]:
                        print(f'UI {width}x{height} {kind}', file=sys.stderr, flush=True)
                        load(path)
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), (width,height,kind,'overflow')
                        columns = page.locator(grid).evaluate('e => getComputedStyle(e).gridTemplateColumns.split(" ").length')
                        expected = 2 if width >= 900 or (width >= 600 and width/height <= 1.3) else 1
                        assert columns == expected, (width,height,kind,columns,expected)
                        assert page.locator('.topbar').bounding_box()['width'] <= width+1
                        assert page.locator('.bottom-bar').bounding_box()['width'] <= width+1
                        if kind == 'report':
                            assert page.locator('#sec-hot [data-issue-card]').count() == 10
                            assert page.locator('#report-content').is_visible()
                            assert page.locator('[data-cve-card]:visible').count() == 20
                            assert page.locator('#cve-grid').evaluate('e => getComputedStyle(e).gridTemplateColumns.split(" ").length') == expected

                        if screenshots and width in (390,768):
                            page.screenshot(path=str(screenshots / f'{kind}-{width}.png'), full_page=(kind=='archive'))
                        results.append({'page':kind,'width':width,'height':height,'columns':columns,'overflow':False})
                print('Functional checks', file=sys.stderr, flush=True)
                page.set_viewport_size({'width':390,'height':844})
                load('index.html')
                assert page.locator('[data-report-card]').count() == 5
                if args.in_memory:
                    target = page.locator('[data-report-card]').first.get_attribute('href')
                    assert target.startswith('reports/')
                    load(target)
                else:
                    page.locator('[data-report-card]').first.click()
                    assert '/project-prefix/reports/' in page.url
                page.locator('[data-bookmark]').click()
                assert page.locator('[data-bookmark]').get_attribute('aria-pressed') == 'true'
                if args.in_memory:
                    assert page.locator('.back-button').get_attribute('href') == '../index.html'
                    load('index.html')
                else:
                    page.locator('.back-button').click()
                page.locator('[data-archive-filter="saved"]').click()
                assert page.locator('[data-report-card]:visible').count() == 1
                if args.in_memory:
                    load(page.locator('[data-report-card]:visible').get_attribute('href'))
                else:
                    page.locator('[data-report-card]:visible').click()
                page.locator('#issue-search').fill('존재하지않는검색어123456789')
                assert page.locator('[data-issue-card]:visible').count() == 0
                page.locator('#issue-search').fill('')
                assert page.locator('[data-issue-card]:visible').count() == 42
                page.locator('[data-cve-more]').click()
                assert page.locator('[data-cve-card]:visible').count() == 40
                page.locator('#issue-search').fill('CVE-2099-90044')
                assert page.locator('[data-cve-card]:visible').count() == 1
                assert page.locator('[data-cve-more]').is_hidden()
                page.locator('#issue-search').fill('')
                assert page.locator('[data-cve-card]:visible').count() == 40
                page.locator('[data-cve-more]').click()
                assert page.locator('[data-cve-card]:visible').count() == 45
                assert page.locator('[data-cve-more]').is_hidden()
                page.locator('.cve-details summary').first.click()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
                if screenshots:
                    for width, height in ((390,844),(768,900)):
                        page.set_viewport_size({'width':width,'height':height})
                        page.locator('#sec-cve').evaluate('e => e.scrollIntoView()')
                        page.screenshot(path=str(screenshots/f'cve-{width}.png'))
                page.set_viewport_size({'width':390,'height':844})
                if args.in_memory:
                    page.locator('#sec-security').evaluate('e => e.scrollIntoView()')
                else:
                    page.locator('.nav-tab[href="#sec-security"]').click()
                    assert page.url.endswith('#sec-security')
                    page.wait_for_timeout(80)
                    assert page.locator('#sec-security').bounding_box()['y'] >= 60
                page.evaluate("Object.defineProperty(navigator, 'share', {value: async x => {window.sharedUrl=x.url;}, configurable: true})")
                page.locator('[data-share]').click()
                assert page.evaluate('window.sharedUrl === location.href')
                page.set_viewport_size({'width':768,'height':900})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
                load('404.html')
                assert page.locator('a').first.get_attribute('href') in ('./','./index.html','index.html', '/project-prefix/', '/project-prefix/index.html')
                # Real HTTP mode also checks reading with JavaScript disabled.
                # The in-memory adapter relies on scripting to inject the document.
                if not args.in_memory:
                    no_js = browser.new_context(java_script_enabled=False)
                    no_js_page = no_js.new_page()
                    no_js_page.goto(base+f'reports/{now.date().isoformat()}.html')
                    assert no_js_page.locator('[data-issue-card]').count() == 67
                    no_js.close()
                assert not errors, errors
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
    report = {'tested_at':now.isoformat(),'browser':'headless Chromium','physical_device_test':False, 'http_navigation_test':not args.in_memory,
              'local_storage_mocked':args.in_memory,
              'viewport_page_checks':len(results),'functional_checks':'bookmark/search/share/resize and static navigation links' if args.in_memory else 'navigation/bookmark/search/anchors/share/resize/no-JS',
              'cve_checks':'12 viewport column/overflow checks; more20/search beyond hidden cards/details', 'page_errors':errors, 'viewports':results}
    if screenshots:
        (screenshots/'results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__ == '__main__': main()
