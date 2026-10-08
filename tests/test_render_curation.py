"""Offline public rendering contracts for saved Gemini curation results."""
from copy import deepcopy
from datetime import datetime, timedelta

from bs4 import BeautifulSoup

from digest.common import CATEGORIES, KST, load_config, read_json
from digest.cves import normalize_cve
from digest.pipeline import empty_state, new_day
from digest.render import render_context, render_site


NOW = datetime(2026, 10, 4, 6, tzinfo=KST)


def curation(rank, reason='제공된 근거에 따른 파급력과 화제성, 이슈성을 고려했습니다.'):
    return {'rank': rank, 'reason_ko': reason, 'related_ids': [],
            'scores': {'social_impact': 4, 'attention': 2, 'issue_relevance': 5}}


def metadata(selected_count, status='fresh'):
    return {'status': status, 'at': NOW.isoformat(), 'limit': 20,
            'model': 'offline-test-model', 'candidate_count': selected_count + 50,
            'selected_count': selected_count}


def article(ident, category='ai', rank=None, published=NOW, stars=1):
    row = {'id': ident, 'url': 'https://example.invalid/' + ident,
           'category': category, 'kind': 'github' if category == 'github' else 'article',
           'source': '오프라인 검증', 'region': 'KR', 'translation': False,
           'title_ko': '선별 화면 검증 기사 ' + ident, 'summary_ko': '실제 기사가 아닌 오프라인 검증 내용입니다.',
           'published_at': published.isoformat(), 'collected_at': NOW.isoformat(),
           'evidence_kind': 'rss', 'repo_name': 'offline/' + ident,
           'stars_today': stars, 'total_stars': stars * 2,
           'programming_language': 'Python', 'observed_at': NOW.isoformat()}
    if rank is not None:
        row['curation'] = curation(rank)
    return row


def cve(index, score=7.5, rank=None):
    raw = {'id': f'CVE-2099-{90000 + index}', 'published': NOW.isoformat(),
           'descriptions': [{'lang': 'en', 'value': 'SYNTHETIC OFFLINE TEST, NOT A REAL CVE.'}],
           'metrics': {'cvssMetricV31': [{'source': 'offline@example.invalid', 'type': 'Primary',
               'cvssData': {'version': '3.1', 'baseScore': score, 'baseSeverity': 'HIGH'}}]}}
    row = normalize_cve(raw, NOW)
    row['summary_ko'] = '실제 CVE가 아닌 오프라인 화면 검증 내용입니다.'
    if rank is not None:
        row['curation'] = curation(rank)
    return row


def render(day, tmp_path):
    state = empty_state()
    day['updated_at'] = NOW.isoformat()
    state['days'][day['date']] = day
    render_site(state, tmp_path, NOW, load_config())
    return BeautifulSoup((tmp_path / 'reports' / (day['date'] + '.html')).read_text(encoding='utf-8'), 'html.parser')


def test_saved_ranks_override_dates_stars_and_cvss_without_changing_state():
    day = new_day(NOW.date().isoformat())
    rows = [article('latest-legacy'), article('lower', rank=2),
            article('important', rank=1, published=NOW - timedelta(hours=2)),
            article('stars', 'github', rank=2, stars=10000),
            article('repo-important', 'github', rank=1, stars=1)]
    day['articles'] = {row['id']: row for row in rows}
    cves = [cve(1, score=10, rank=2), cve(2, score=4, rank=1), cve(3, score=9)]
    day['cves'] = {row['id']: row for row in cves}
    original = deepcopy(day)
    view = render_context(day, day['date'])
    assert [row['id'] for row in view['sections']['ai']] == ['important', 'lower', 'latest-legacy']
    assert [row['id'] for row in view['sections']['github']] == ['repo-important', 'stars']
    assert [row['id'] for row in view['cve_cards']] == ['CVE-2099-90002', 'CVE-2099-90001', 'CVE-2099-90003']
    assert day == original


def test_legacy_records_retain_their_original_sorting():
    day = new_day(NOW.date().isoformat())
    older, newer = article('older', published=NOW - timedelta(hours=1)), article('newer')
    day['articles'] = {row['id']: row for row in [older, newer, article('repo-small', 'github', stars=1),
                                              article('repo-large', 'github', stars=50)]}
    day['cves'] = {row['id']: row for row in [cve(1, score=4), cve(2, score=10)]}
    view = render_context(day, day['date'])
    assert [row['id'] for row in view['sections']['ai']] == ['newer', 'older']
    assert [row['id'] for row in view['sections']['github']] == ['repo-large', 'repo-small']
    assert [row['id'] for row in view['cve_cards']] == ['CVE-2099-90002', 'CVE-2099-90001']


def test_curated_report_explains_policy_escapes_reasons_and_links_related_cves(tmp_path):
    day = new_day(NOW.date().isoformat())
    day['news_curation'], day['cve_curation'] = metadata(1), metadata(1)
    row = article('picked', rank=1)
    row['curation']['reason_ko'] = '<script>alert(1)</script> 한국어 선정 이유'
    day['articles'][row['id']] = row
    vulnerability = cve(1, rank=1)
    vulnerability['related_cves'] = [
        {'id': 'CVE-2099-99998', 'url': 'https://nvd.nist.gov/vuln/detail/CVE-2099-99998', 'cvss_score': 9.1},
        {'id': 'CVE-2099-99999', 'url': 'https://nvd.nist.gov/vuln/detail/CVE-2099-99999', 'cvss_score': None}]
    day['cves'][vulnerability['id']] = vulnerability
    page = render(day, tmp_path)
    text = page.get_text(' ', strip=True)
    assert '분야마다 하루 최대 20건' in text
    assert '사회적 파급력·화제성·이슈성' in text
    assert 'Gemini의 중요도 평가와 별개' in text
    assert '함께 묶인 관련 CVE 2건' in text
    assert '사회적 파급력 4/5' in text
    assert not page.select_one('#sec-ai script')
    assert '<script>alert(1)</script> 한국어 선정 이유' in page.select_one('#sec-ai .pick-reason').get_text()
    link = page.select_one('a[href="https://nvd.nist.gov/vuln/detail/CVE-2099-99998"]')
    assert link and {'noopener', 'noreferrer'} <= set(link['rel'])
    assert '점수 미제공' in page.select_one('.cve-details').get_text()


def test_legacy_report_does_not_claim_a_gemini_curation_policy(tmp_path):
    day = new_day(NOW.date().isoformat())
    day.pop('news_curation', None)
    day.pop('cve_curation', None)
    day['articles']['legacy'] = article('legacy')
    row = cve(1)
    day['cves'][row['id']] = row
    page = render(day, tmp_path)
    text = page.get_text(' ', strip=True)
    assert '선별 정책을 적용하기 전에 생성된 기록' in text
    assert '선별 정책을 적용하기 전의 기록' in text
    assert 'Gemini가 중복 보도를 정리하고' not in text
    assert 'Gemini 선정 이유' not in text


def test_stale_selection_has_specific_notice(tmp_path):
    day = new_day(NOW.date().isoformat())
    day['news_curation'], day['cve_curation'] = metadata(1, 'stale'), metadata(1, 'stale')
    day['articles']['last-good'] = article('last-good', rank=1)
    row = cve(1, rank=1)
    day['cves'][row['id']] = row
    text = render(day, tmp_path).get_text(' ', strip=True)
    assert '기사 재선정에 실패하여' in text
    assert 'CVE 재선정에 실패하여' in text


def test_public_counts_use_selected_cards_and_exclude_related_cve_links(tmp_path):
    day = new_day(NOW.date().isoformat())
    day['news_curation'], day['cve_curation'] = metadata(100), metadata(20)
    for category in CATEGORIES:
        for index in range(20):
            row = article(f'{category}-{index}', category, rank=index + 1)
            day['articles'][row['id']] = row
    for index in range(20):
        row = cve(index, rank=index + 1)
        row['related_cves'] = [{'id': 'CVE-2099-99999',
                              'url': 'https://nvd.nist.gov/vuln/detail/CVE-2099-99999', 'cvss_score': 9}]
        day['cves'][row['id']] = row
    page = render(day, tmp_path)
    for category in CATEGORIES:
        assert len(page.select(f'#sec-{category} [data-issue-card]')) == 20
    assert len(page.select('[data-cve-card]')) == 20
    report = read_json(tmp_path / 'reports.json', {})['reports'][0]
    assert report['count'] == 100 and report['cve_count'] == 20
    assert 'CVE 20건' in (tmp_path / 'index.html').read_text(encoding='utf-8')


def test_independent_latest_preserves_order_unique_counts_and_html_ids(tmp_path):
    day = new_day(NOW.date().isoformat())
    common = article('common', rank=1, published=NOW - timedelta(hours=2))
    day['articles'][common['id']] = common
    day['hot'] = [{'id': common['id'], 'reason_ko': '오프라인 HOT 선정'}]
    latest = [article(f'fresh-{index}', rank=20 - index,
                      published=NOW - timedelta(minutes=index)) for index in range(25)]
    day['latest_articles'] = {row['id']: row for row in latest + [common]}
    day['latest_curation'] = metadata(20)
    page = render(day, tmp_path)
    cards = page.select('#sec-latest [data-issue-card]')
    assert [card['id'] for card in cards] == [f'latest-fresh-{index}' for index in range(20)]
    assert not page.select('#sec-latest .pick-reason')
    assert len(page.select('#sec-ai [data-issue-card]')) == 1
    ids = [node['id'] for node in page.select('[id]')]
    assert len(ids) == len(set(ids))
    report = read_json(tmp_path / 'reports.json', {})['reports'][0]
    assert report['count'] == 21 and report['section_count'] == 1
    assert report['latest_count'] == report['counts']['latest'] == 20


def test_historical_latest_uses_original_collection_time_and_excludes_undated(tmp_path):
    day = new_day(NOW.date().isoformat())
    good = article('good', published=NOW - timedelta(hours=1))
    paper = article('paper'); paper['kind'] = 'paper'
    missing = article('missing'); missing['published_at'] = None
    rows = [good, paper, missing, article('future', published=NOW + timedelta(minutes=1)),
            article('expired', published=NOW - timedelta(days=2)), article('repo', 'github')]
    day['articles'] = {row['id']: row for row in rows}
    day.pop('latest_articles', None)
    day['updated_at'] = NOW.isoformat()
    state = empty_state(); state['days'][day['date']] = day
    render_site(state, tmp_path, NOW + timedelta(days=3), load_config())
    page = BeautifulSoup((tmp_path / 'reports' / (day['date'] + '.html')).read_text(encoding='utf-8'), 'html.parser')
    assert [node['id'] for node in page.select('#sec-latest [data-issue-card]')] == ['latest-good']
