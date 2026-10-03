"""API preflight exercises real curation schemas without collecting or publishing."""
from copy import deepcopy

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
    monkeypatch.setattr(cli, 'Gemini', DiagnosticClient)
    for name in ('load_state', 'run_pipeline', 'render_site', 'write_json',
                 'collect_sources', 'collect_github', 'collect_cves'):
        monkeypatch.setattr(cli, name, forbidden)
    assert cli.main() == 0
    selections = [call for call in calls if 'candidates' in call[0]]
    assert len(selections) == 2
    assert all(call[2] == 'gemini-3.8-flash' and call[3]['max_output_tokens'] == 16384
               for call in selections)
    assert 'category' in selections[0][1]['properties']['picks']['items']['properties']
    assert 'category' not in selections[1][1]['properties']['picks']['items']['properties']
