from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from pathlib import Path

from .common import ROOT, load_config, now_kst, read_json, write_json
from .gemini import Gemini, GeminiError, GeminiAuthenticationError
from .cves import collect_cves
from .curation import curate_candidates
from .latest import select_latest_candidates
from .network import PublicWeb
from .pipeline import load_state, prune, run_pipeline
from .render import render_site
from .sources import collect_sources, collect_github
from .publication import aware_date


def main():
    parser = argparse.ArgumentParser(description='AI·보안 뉴스 → Gemini → 한국어 HTML 보고서')
    parser.add_argument('--state-dir', type=Path, default=ROOT / 'state')
    parser.add_argument('--output', type=Path, default=ROOT / 'public')
    parser.add_argument('--schedule', default=os.getenv('SCHEDULE_CRON', ''))
    parser.add_argument('--publication-at', default=os.getenv('PUBLISH_AT', ''),
                        help='날짜와 시간대를 포함한 예약 공개 목표 시각')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--build-only', action='store_true', help='API/수집 없이 현재 상태로 HTML만 생성')
    mode.add_argument('--check-sources', action='store_true', help='수집원·NVD 확인; Gemini 호출·상태 변경 없음')
    mode.add_argument('--check-api', action='store_true', help='설정한 Gemini 요약·선별·HOT 모델 연결 확인')
    mode.add_argument('--check-curation', action='store_true',
                      help='저장된 최신 후보 전체로 뉴스·CVE 선별 검사; 상태 변경·배포 없음')
    args = parser.parse_args()
    cfg, now = load_config(), now_kst()
    if args.check_sources:
        web = PublicWeb(cfg['user_agent'], cfg['http_timeout_seconds'], cfg['per_host_delay_seconds'])
        _, statuses = collect_sources(web, read_json(ROOT / 'config/sources.json', []), now, cfg)
        _, gh_status = collect_github(web, now, cfg)
        statuses.append(gh_status)
        if cfg.get('cve_enabled'):
            _, cve_status = collect_cves(now, cfg)
            statuses.append(cve_status)
        print(json.dumps(statuses, ensure_ascii=False, indent=2))
        return 0 if any(s['status'] == 'ok' for s in statuses) else 1
    if args.check_api:
        client = Gemini(cfg)
        schema = {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok']}
        for model in dict.fromkeys((client.summary_model, cfg.get('curation_model', 'gemini-3.8-flash'), client.hot_model)):
            client.request('연결 시험입니다. {"ok":true}만 반환하세요.', {}, schema, model)
            print(f'{model}: 연결 확인')
        # Test the production selection schemas with tiny synthetic inputs too.
        # Simple {ok:true} checks cannot detect provider schema rejections.
        for kind, candidate in (
            ('news', {'id': 'diagnostic-news', 'kind': 'article', 'title_original': 'API 연결 검사 자료',
                      'excerpt': '실제 뉴스가 아닌 가상의 연결 검사 자료입니다.'}),
            ('cve', {'id': 'CVE-2026-00001', 'description': 'Synthetic API connectivity test only.'}),
        ):
            curate_candidates(client, [candidate], kind=kind,
                              model=cfg.get('curation_model', 'gemini-3.8-flash'), as_of=now.isoformat())
            print(f'{kind}: 실제 선별 요청 형식 확인')
        select_latest_candidates(client, [{'id': 'diagnostic-latest', 'kind': 'article',
            'url': 'https://example.invalid/diagnostic', 'published_at': now.isoformat(),
            'title_original': '실제 뉴스가 아닌 API 연결 검사 자료',
            'excerpt': '실제 뉴스가 아닌 가상의 연결 검사 자료입니다.'}], now,
            model=cfg.get('curation_model', 'gemini-3.8-flash'))
        print('latest: 실제 최신 선별 요청 형식 확인')
        return 0
    if args.check_curation:
        # Read the checkpoint directly: never prune, collect, save or render
        # during this diagnostic. Replays exercise production-size requests.
        checkpoint = read_json(args.state_dir / 'state.json', {})
        days = checkpoint.get('days', {})
        if not days:
            raise GeminiError('선별 검사에 사용할 저장된 후보가 없습니다')
        date = max(days)
        day = days[date]
        client = Gemini(cfg)
        failed = False
        for kind in ('news', 'cve'):
            candidates = list(day.get(f'{kind}_candidates', {}).values())
            if not candidates:
                print(f'{date} {kind}: 저장된 후보 없음')
                continue
            before = client.calls
            try:
                result = curate_candidates(client, candidates, kind=kind,
                    model=cfg.get('curation_model', 'gemini-3.8-flash'),
                    limit=cfg.get('category_daily_limit', 20), as_of=now.isoformat())
                counts = Counter(pick.get('category', 'cve') for pick in result['picks'])
                print(f'{date} {kind}: 후보 {len(candidates)}건 · 선정 {len(result["picks"])}건 · '
                      f'분야별 {dict(counts)} · 요청 {client.calls - before}회')
            except GeminiAuthenticationError:
                raise
            except GeminiError as exc:
                # Gemini errors use fixed diagnostic labels, never raw output.
                print(f'{date} {kind}: 선별 검사 실패 · {exc}')
                failed = True
        before = client.calls
        try:
            result = select_latest_candidates(client, list(day.get('news_candidates', {}).values()), now,
                model=cfg.get('curation_model', 'gemini-3.8-flash'), limit=cfg.get('latest_daily_limit', 20))
            print(f'{date} latest: 적격 {result["candidate_count"]}건 · '
                  f'최신 {result["considered_count"]}건 확인 · 선정 {len(result["picks"])}건 · '
                  f'요청 {client.calls - before}회')
        except GeminiAuthenticationError:
            raise
        except GeminiError as exc:
            print(f'{date} latest: 선별 검사 실패 · {exc}')
            failed = True
        print(f'선별 검사 합계: 요청 {client.calls}회 · API 보고 토큰 {client.tokens}')
        return 1 if failed else 0
    state = load_state(args.state_dir)
    prune(state, now, cfg['keep_days'])
    if not args.build_only:
        report = run_pipeline(state, args.state_dir, now, cfg, args.schedule,
                              publication_at=aware_date(args.publication_at) if args.publication_at else None)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        summary_path = os.getenv('GITHUB_STEP_SUMMARY')
        if summary_path:
            text = (f'## 수집 결과\n\nKST {report["at"]}\n\n'
                    f'신규 {report["new_count"]}건 · 보류 {report["pending_count"]}건 · '
                    f'Gemini 요청 {report["api_calls"]}회 · API 보고 토큰 {report["api_tokens"]}\n\n'
                    f'당일 NVD 공개 CVE {report.get("cve_count", 0)}건 · CVE 번역 대기 '
                    f'{report.get("cve_summary", {}).get("pending", 0)}건(오늘 선정분)\n\n'
                    f'Gemini 선별 {report.get("curation_model", "")} · 카테고리별 하루 최대 20건\n\n')
            text += f'최신 기사 {report.get("latest_count", 0)}건 · 발행시각순 최대 20건\n\n'
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
