"""API preflight exercises real curation schemas without collecting or publishing."""
from copy import deepcopy
import pytest
from digest.common import write_json
from digest.openai_client import OpenAIError

from digest import __main__ as cli


def test_api_preflight_checks_both_selection_schemas_without_state_or_site(monkeypatch):
    calls = []

    class DiagnosticClient:
        summary_model, hot_model = 'test-summary', 'test-hot'
        calls, tokens, remaining = 0, 0, 80

        def __init__(self, cfg):
            pass

        def request(self, instruction, data, schema, model, validator=None, **kwargs):
            calls.append((deepcopy(data), deepcopy(schema), model, kwargs))
            self.calls += 1
            result = ({'picks': [], 'shortfall_reason_ko': '실제 기사나 취약점이 아닌 연결 검사 자료입니다.'}
                      if 'candidates' in data else {'ok': True})
            if validator:
                validator(result)
            return result

    def forbidden(*args, **kwargs):
        raise AssertionError('API preflight must never collect, save state or publish')

    monkeypatch.setattr('sys.argv', ['digest', '--check-api'])
    monkeypatch.setattr(cli, 'OpenAIClient', DiagnosticClient)
    for name in ('load_state', 'run_pipeline', 'render_site', 'write_json',
                 'collect_sources', 'collect_github', 'collect_cves'):
        monkeypatch.setattr(cli, name, forbidden)
    assert cli.main() == 0
    selections = [call for call in calls if 'candidates' in call[0]]
    assert len(selections) == 3
    assert all(call[2] == 'gpt-6-luna' and call[3]['max_output_tokens'] == 32768
               and 'reasoning_effort' not in call[3]
               for call in selections[:2])
    assert 'category' in selections[0][1]['properties']['picks']['items']['properties']
    assert 'category' not in selections[1][1]['properties']['picks']['items']['properties']
    assert set(selections[2][1]['properties']['picks']['items']['properties']) == {'id', 'related_ids'}
    assert selections[2][3]['max_output_tokens'] == 8192


@pytest.mark.parametrize('failure', [False, True])
def test_saved_pool_diagnostic_is_read_only_and_returns_failure(monkeypatch, tmp_path, capsys, failure):
    checkpoint = {'days': {'2026-10-06': {
        'news_candidates': {'news-id': {'id': 'news-id', 'kind': 'article'}},
        'cve_candidates': {'cve-id': {'id': 'cve-id'}}}}}
    path = tmp_path / 'state.json'
    write_json(path, checkpoint)
    original = path.read_bytes()
    calls = []

    class Client:
        calls, tokens = 0, 0
        def __init__(self, cfg):
            pass

    def select(client, candidates, **kwargs):
        calls.append((deepcopy(candidates), kwargs))
        client.calls += 1
        if failure and kwargs['kind'] == 'news':
            raise OpenAIError('고정된 진단 오류')
        return {'picks': [], 'shortfall_reason_ko': '진단 자료입니다.'}

    def forbidden(*args, **kwargs):
        raise AssertionError('Diagnostic must not collect, prune, save or publish')

    monkeypatch.setattr('sys.argv', ['digest', '--check-curation', '--state-dir', str(tmp_path)])
    monkeypatch.setattr(cli, 'OpenAIClient', Client)
    monkeypatch.setattr(cli, 'curate_candidates', select)
    for name in ('load_state', 'prune', 'run_pipeline', 'render_site', 'write_json',
                 'collect_sources', 'collect_github', 'collect_cves'):
        monkeypatch.setattr(cli, name, forbidden)
    assert cli.main() == int(failure)
    assert path.read_bytes() == original
    assert [call[1]['kind'] for call in calls] == ['news', 'cve']
    assert calls[0][0] == list(checkpoint['days']['2026-10-06']['news_candidates'].values())
    assert all(call[1]['model'] == 'gpt-6-luna' and call[1]['limit'] == 40 for call in calls)
    assert '선별 검사 합계' in capsys.readouterr().out


def test_saved_pool_diagnostic_requires_checkpoint(monkeypatch, tmp_path):
    monkeypatch.setattr('sys.argv', ['digest', '--check-curation', '--state-dir', str(tmp_path)])
    with pytest.raises(OpenAIError, match='저장된 후보'):
        cli.main()
