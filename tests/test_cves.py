"""CVE contracts and regressions: synthetic data only, no external network/API keys."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import Mock

import pytest
import requests
from bs4 import BeautifulSoup

from digest.common import KST, load_config
from digest.cves import (NvdClient, NvdError, ENDPOINT, collect_cves, cvss_metrics,
                        merge_cves, normalize_cve, query_window, translate_cves, utc_parameter)
from digest.pipeline import empty_state, new_day, run_pipeline, load_state, prune
from digest.render import render_site, render_context
from digest.gemini import GeminiError

NOW = datetime(2026, 9, 22, 13, 0, tzinfo=KST)


def raw(index=1, published='2026-09-21T15:00:00.000', score=7.5, **extra):
    result = {'id': f'CVE-2026-{index:05}', 'published': published,
              'lastModified': '2026-09-22T03:00:00.000', 'vulnStatus': 'Awaiting Analysis',
              'descriptions': [{'lang': 'en', 'value': 'SYNTHETIC TEST ONLY. Example server accepts untrusted input that can cause a denial of service.'}],
              'metrics': {'cvssMetricV31': [{'source': 'vendor@example.invalid', 'type': 'Primary',
                 'cvssData': {'version': '3.1', 'baseScore': score, 'baseSeverity': 'HIGH',
                              'vectorString': 'CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:H'}}]}}
    result.update(extra)
    return result


def page(rows, total=None, index=0):
    return {'vulnerabilities': [{'cve': r} for r in rows],
            'totalResults': len(rows) if total is None else total, 'startIndex': index}


def status(state='ok'):
    start, end = query_window(NOW, 5)
    return {'name': 'NVD', 'status': state, 'count': 0, 'message': '오프라인 시험',
            'start': start.isoformat(), 'end': end.isoformat()}


class FakeSummary:
    summary_model = hot_model = 'offline-test-model'
    tokens = 0
    def __init__(self, budget=80, broken=False):
        self.calls, self.budget, self.broken, self.inputs = 0, budget, broken, []
    @property
    def remaining(self):
        return self.budget - self.calls
    def request(self, instruction, data, schema, model):
        self.calls += 1
        self.inputs.append(deepcopy(data))
        if self.broken:
            raise GeminiError('검증용 실패')
        return {'cves': [{'id': row['id'], 'summary_ko': '실제 취약점이 아닌 테스트용 한국어 설명입니다.'}
                         for row in data['cves']]}
    def select_hot(self, articles):
        self.calls += 1
        return {'picks': [], 'shortfall_reason_ko': ''}


def test_kst_midnight_to_utc_query():
    start, end = query_window(NOW, 1)
    assert utc_parameter(start) == '2026-09-21T15:00:00.000Z'
    assert utc_parameter(end) == '2026-09-22T04:00:00.000Z'
    assert query_window(NOW, 5)[0].date().isoformat() == '2026-09-18'


@pytest.mark.parametrize('days', [0, 121])
def test_bad_query_range(days):
    with pytest.raises(ValueError): query_window(NOW, days)


def test_no_timezone_disallowed():
    with pytest.raises(ValueError): query_window(NOW.replace(tzinfo=None), 5)


def test_published_not_modified_or_id_year():
    row = normalize_cve(raw(published='2026-09-20T14:59:59.999', id='CVE-2024-12345'), NOW)
    assert row['published_day'] == '2026-09-20'
    assert row['last_modified_at'].startswith('2026-09-22')
    assert row['id'].startswith('CVE-2024-')
    assert normalize_cve(raw(), NOW)['published_day'] == '2026-09-22'


@pytest.mark.parametrize('score', [None, True, float('nan'), float('inf'), -1, 11, '9.8'])
def test_invalid_or_missing_score_not_guessed(score):
    assert normalize_cve(raw(score=score), NOW)['cvss'] is None


def test_real_zero_score_is_preserved():
    assert normalize_cve(raw(score=0), NOW)['cvss']['score'] == 0


def test_preferred_version_source_not_highest():
    metrics = raw()['metrics']
    metrics['cvssMetricV31'].append({'source':'nvd@nist.gov', 'type':'Primary',
              'cvssData': {'version':'3.1', 'baseScore': 6.0}})
    assert cvss_metrics(metrics)[0]['source'] == 'nvd@nist.gov'
    metrics['cvssMetricV40'] = [{'source':'vendor@example.invalid', 'type':'Secondary',
              'cvssData': {'version':'4.0', 'baseScore': 4.3}}]
    results = cvss_metrics(metrics)
    assert results[0]['score'] == 4.3 and results[0]['version'] == '4.0'
    assert len(results) == 3
    assert results[0]['severity'] == 'UNKNOWN'


def test_inconsistent_metric_version_rejected():
    metrics = raw()['metrics']
    metrics['cvssMetricV31'][0]['cvssData']['version'] = '4.0'
    assert cvss_metrics(metrics) == []


def test_all_pages_and_id_deduplication():
    client = Mock()
    client.get_page.side_effect = [page([raw(1), raw(2)], 4), page([raw(2), raw(3)], 4, 2)]
    rows, result = collect_cves(NOW, load_config(), client)
    assert len(rows) == 3 and result['status'] == 'ok' and result['pages'] == 2
    assert client.get_page.call_args_list[1].args[0]['startIndex'] == 2
    assert 'noRejected' not in client.get_page.call_args_list[0].args[0]  # need rejections to remove stale cards


def test_page_failure_keeps_successful_prefix():
    client = Mock()
    client.get_page.side_effect = [page([raw()], 3), NvdError('임시 오류')]
    rows, result = collect_cves(NOW, load_config(), client)
    assert len(rows) == 1 and result['status'] == 'partial' and '임시 오류' in result['message']


def test_api_failure_distinct_from_zero_results():
    client = Mock()
    client.get_page.return_value = page([])
    assert collect_cves(NOW, load_config(), client)[1]['status'] == 'ok'
    client.get_page.side_effect = NvdError('HTTP 403')
    assert collect_cves(NOW, load_config(), client)[1]['status'] == 'error'


def test_changed_total_is_partial():
    client = Mock()
    client.get_page.side_effect = [page([raw(1)], 3), page([raw(2)], 2, 1)]
    assert collect_cves(NOW, load_config(), client)[1]['status'] == 'partial'


def test_page_index_mismatch_detected():
    client = Mock(get_page=Mock(return_value=page([raw()], 1, 4)))
    assert collect_cves(NOW, load_config(), client)[1]['status'] == 'error'


def test_bad_record_is_not_silent_success():
    client = Mock(get_page=Mock(return_value=page([raw(), raw(id='NOT-CVE')], 2)))
    rows, result = collect_cves(NOW, load_config(), client)
    assert len(rows) == 1 and result['invalid_count'] == 1 and result['status'] == 'partial'


def test_recheck_window_filters_future_and_expired():
    client = Mock(get_page=Mock(return_value=page([
        raw(1, published='2026-09-10T00:00:00Z'),
        raw(2), raw(3, published='2026-09-22T04:00:01Z')])))
    assert [x['id'] for x in collect_cves(NOW, load_config(), client)[0]] == ['CVE-2026-00002']


def test_safety_page_cap_is_visible():
    cfg = load_config(); cfg['nvd_max_pages_per_run'] = 1
    client = Mock(get_page=Mock(return_value=page([raw()], 100)))
    assert collect_cves(NOW, cfg, client)[1]['status'] == 'partial'


def test_older_backfill_stays_on_its_publication_day():
    state, day = empty_state(), new_day('2026-09-22')
    old = normalize_cve(raw(published='2026-09-21T12:30:00Z'), NOW)
    merge_cves(state, day, [old], status(), NOW, new_day)
    assert not day['cves']
    assert old['id'] in state['days']['2026-09-21']['cves']


def test_reuse_translation_on_score_change_only():
    state, day = empty_state(), new_day('2026-09-22')
    row = normalize_cve(raw(), NOW); row['summary_ko'] = '캐시된 한국어 요약'
    day['cves'][row['id']] = row
    merge_cves(state, day, [normalize_cve(raw(score=8.1), NOW)], status(), NOW, new_day)
    assert day['cves'][row['id']]['summary_ko'] == '캐시된 한국어 요약'
    assert day['cves'][row['id']]['cvss']['score'] == 8.1
    changed = raw(); changed['descriptions'][0]['value'] += ' Changed description.'
    merge_cves(state, day, [normalize_cve(changed, NOW)], status(), NOW, new_day)
    assert day['cves'][row['id']]['summary_ko'] == ''


def test_rejection_removes_cached_card():
    state, day = empty_state(), new_day('2026-09-22')
    row = normalize_cve(raw(), NOW); day['cves'][row['id']] = row
    merge_cves(state, day, [normalize_cve(raw(vulnStatus='Rejected'), NOW)], status(), NOW, new_day)
    assert not day['cves']


def test_failed_refresh_preserves_data_and_success_time():
    state, day = empty_state(), new_day('2026-09-22')
    merge_cves(state, day, [normalize_cve(raw(), NOW)], status(), NOW, new_day)
    successful = day['cve_meta']['last_success_at']
    merge_cves(state, day, [], status('error'), NOW + timedelta(hours=6), new_day)
    assert day['cves'] and day['cve_meta']['last_success_at'] == successful
    assert day['cve_meta']['status'] == 'error'


def test_translation_never_receives_scores_and_retains_all_rows():
    state, day, client = empty_state(), new_day('2026-09-22'), FakeSummary()
    for i in range(30):
        row = normalize_cve(raw(i), NOW); day['cves'][row['id']] = row
    cfg = load_config(); cfg['cve_summary_max_calls_per_run'] = 3
    check = Mock()
    result = translate_cves(state, day, NOW, cfg, client, check)
    assert result['translated'] == 12 and result['pending'] == 18
    assert len(day['cves']) == 30 and check.call_count == 1
    assert all(set(x) == {'id','description'} for x in client.inputs[0]['cves'])


def test_translation_failure_does_not_remove_cves():
    state, day = empty_state(), new_day('2026-09-22')
    row = normalize_cve(raw(), NOW); day['cves'][row['id']] = row
    result = translate_cves(state, day, NOW, load_config(), FakeSummary(broken=True), Mock())
    assert result['error'] and result['pending'] == 1 and len(day['cves']) == 1


def test_today_translations_before_backfill():
    state, day, client = empty_state(), new_day('2026-09-22'), FakeSummary()
    yesterday = new_day('2026-09-21')
    yesterday['cves']['older'] = normalize_cve(raw(2, published='2026-09-21T01:00:00Z', score=10), NOW)
    state['days']['2026-09-21'] = yesterday
    row = normalize_cve(raw(1, score=1), NOW); day['cves'][row['id']] = row
    cfg = load_config(); cfg['cve_summary_batch_size'] = 1; cfg['cve_summary_max_calls_per_run'] = 3
    translate_cves(state, day, NOW, cfg, client, Mock())
    assert client.inputs[0]['cves'][0]['id'] == row['id']


def test_summary_id_invention_rejected_atomically():
    client = FakeSummary()
    client.request = Mock(return_value={'cves':[{'id':'CVE-2026-99999','summary_ko':'테스트 설명'}]})
    state, day = empty_state(), new_day('2026-09-22')
    row = normalize_cve(raw(), NOW); day['cves'][row['id']] = row
    result = translate_cves(state, day, NOW, load_config(), client, Mock())
    assert result['error'] and not row['summary_ko']


def pipeline_run(state, path, rows, now=NOW):
    return run_pipeline(state, path, now, load_config(), web=object(), gemini=FakeSummary(),
                        source_loader=lambda *args:([], []),
                        github_loader=lambda *args:([], {'name':'GitHub', 'status':'ok','message':'test','count':0}),
                        cve_loader=lambda *args:(deepcopy(rows),status()))


def test_cve_only_report_three_runs_same_day(tmp_path):
    state = empty_state()
    for index in range(1,4):
        pipeline_run(state, tmp_path, [normalize_cve(raw(index), NOW)], now=NOW+timedelta(hours=index))
    loaded = load_state(tmp_path)
    assert list(loaded['days']) == ['2026-09-22']
    assert len(loaded['days']['2026-09-22']['cves']) == 3
    assert loaded['days']['2026-09-22']['manual_runs'] == 3
    render_site(loaded, tmp_path/'public', NOW, load_config())
    assert len(list((tmp_path/'public/reports').glob('*.html'))) == 1


def test_legacy_v1_state_stays_readable(tmp_path):
    state = empty_state(); day = new_day('2026-09-21'); day.pop('cves'); day.pop('cve_meta')
    state['days'][day['date']] = day
    pipeline_run(state, tmp_path, [normalize_cve(raw(), NOW)])
    assert load_state(tmp_path)['version'] == 1
    assert '2026-09-21' in state['days']


def test_renderer_missing_score_escape_and_versions(tmp_path):
    state = empty_state(); day = new_day('2026-09-22'); day['updated_at'] = NOW.isoformat()
    row = normalize_cve(raw(score=None), NOW)
    row['summary_ko'] = '<script>alert(1)</script> 한국어 검증'; day['cves'][row['id']] = row
    state['days'][day['date']] = day
    render_site(state, tmp_path/'public', NOW, load_config())
    text = (tmp_path/'public/reports/2026-09-22.html').read_text()
    soup = BeautifulSoup(text, 'html.parser')
    assert soup.select_one('#sec-cve') and len(soup.select('[data-cve-card]')) == 1
    assert '점수 미제공' in soup.get_text() and 'CVSS 0.0' not in soup.get_text()
    assert not soup.select_one('#sec-cve script')
    assert 'CVE 1건' in (tmp_path/'public/index.html').read_text()


def test_five_day_retention_prunes_cve_days():
    state = empty_state()
    for index in range(7):
        date = (NOW-timedelta(days=index)).date().isoformat()
        state['days'][date] = new_day(date)
    prune(state, NOW, 5)
    assert len(state['days']) == 5 and min(state['days']) == '2026-09-18'


def response(payload=None, status_code=200):
    obj = Mock(status_code=status_code, headers={})
    obj.__enter__ = Mock(return_value=obj); obj.__exit__ = Mock(return_value=False)
    obj.iter_content.return_value = [json.dumps(payload or page([])).encode()]
    return obj


def test_nvd_header_key_not_query_and_no_redirect(monkeypatch):
    monkeypatch.setenv('NVD_API_KEY', 'fake-test-secret')
    session = Mock(get=Mock(return_value=response()))
    client = NvdClient(load_config(), session)
    client.get_page({'resultsPerPage':2000})
    args, kwargs = session.get.call_args
    assert args == (ENDPOINT,) and kwargs['headers']['apiKey'] == 'fake-test-secret'
    assert 'apiKey' not in kwargs['params'] and kwargs['allow_redirects'] is False
    assert client.interval >= 6


def test_nvd_key_optional(monkeypatch):
    monkeypatch.delenv('NVD_API_KEY', raising=False)
    session = Mock(get=Mock(return_value=response()))
    NvdClient(load_config(), session).get_page({})
    assert 'apiKey' not in session.get.call_args.kwargs['headers']


def test_nvd_retry_and_no_secret_in_errors(monkeypatch):
    monkeypatch.setenv('NVD_API_KEY', 'fake-test-secret')
    monkeypatch.setattr('digest.cves.time.sleep', lambda _:None)
    session = Mock(get=Mock(side_effect=[response(status_code=429), response()]))
    assert NvdClient(load_config(), session).get_page({})['totalResults'] == 0
    assert session.get.call_count == 2
    session.get = Mock(return_value=response(status_code=403))
    with pytest.raises(NvdError) as error: NvdClient(load_config(), session).get_page({})
    assert 'fake-test-secret' not in str(error.value)
