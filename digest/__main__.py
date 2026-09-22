from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .common import ROOT, load_config, now_kst, read_json, write_json
from .gemini import Gemini, GeminiError
from .network import PublicWeb
from .pipeline import load_state, prune, run_pipeline
from .render import render_site
from .sources import collect_sources, collect_github


def main():
    parser = argparse.ArgumentParser(description='AI·보안 뉴스 → Gemini → 한국어 HTML 보고서')
    parser.add_argument('--state-dir', type=Path, default=ROOT / 'state')
    parser.add_argument('--output', type=Path, default=ROOT / 'public')
    parser.add_argument('--schedule', default=os.getenv('SCHEDULE_CRON', ''))
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--build-only', action='store_true', help='API/수집 없이 현재 상태로 HTML만 생성')
    mode.add_argument('--check-sources', action='store_true', help='수집원만 확인; API 호출·상태 변경 없음')
    mode.add_argument('--check-api', action='store_true', help='설정한 Gemini 모델 2개 연결 확인')
    args = parser.parse_args()
    cfg, now = load_config(), now_kst()
    if args.check_sources:
        web = PublicWeb(cfg['user_agent'], cfg['http_timeout_seconds'], cfg['per_host_delay_seconds'])
        _, statuses = collect_sources(web, read_json(ROOT / 'config/sources.json', []), now, cfg)
        _, gh_status = collect_github(web, now, cfg)
        statuses.append(gh_status)
        print(json.dumps(statuses, ensure_ascii=False, indent=2))
        return 0 if any(s['status'] == 'ok' for s in statuses) else 1
    if args.check_api:
        client = Gemini(cfg)
        schema = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok']}
        for model in dict.fromkeys((client.summary_model, client.hot_model)):
            client.request('연결 시험입니다. {"ok":true}만 반환하세요.', {}, schema, model)
            print(f'{model}: 연결 확인')
        return 0
    state = load_state(args.state_dir)
    prune(state, now, cfg['keep_days'])
    if not args.build_only:
        report = run_pipeline(state, args.state_dir, now, cfg, args.schedule)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        summary_path = os.getenv('GITHUB_STEP_SUMMARY')
        if summary_path:
            text = (f'## 수집 결과\n\nKST {report["at"]}\n\n'
                    f'신규 {report["new_count"]}건 · 보류 {report["pending_count"]}건 · '
                    f'Gemini 요청 {report["api_calls"]}회 · API 보고 토큰 {report["api_tokens"]}\n\n')
            text += '\n'.join('- ' + message for message in report['warnings'])
            Path(summary_path).write_text(text + '\n', encoding='utf-8')
    if args.build_only and (args.state_dir / 'state.json').exists():
        write_json(args.state_dir / 'state.json', state)
    render_site(state, args.output, now, cfg)
    print(f'HTML 생성: {args.output / "index.html"}')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except GeminiError as exc:
        raise SystemExit(str(exc)) from None
