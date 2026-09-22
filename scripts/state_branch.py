"""Persist only state.json on a separate Git branch; never commit site sources or secrets."""
from __future__ import annotations
import argparse
import subprocess
from pathlib import Path

BRANCH = 'news-state'


def git(*args: str, cwd: Path | None = None, capture=False):
    return subprocess.run(['git', *args], cwd=cwd, check=True,
                          capture_output=capture, text=True)


def restore(directory: Path):
    directory = directory.resolve()
    if directory.exists():
        raise SystemExit(f'복구 경로가 이미 존재합니다: {directory}. 기존 데이터를 보존하고 경로를 확인하세요.')
    # Failure to contact the remote is fatal, not interpreted as an absent branch.
    result = git('ls-remote', '--heads', 'origin', f'refs/heads/{BRANCH}', capture=True)
    if result.stdout.strip():
        git('fetch', '--depth=1', 'origin', f'refs/heads/{BRANCH}')
        git('worktree', 'add', '--detach', str(directory), 'FETCH_HEAD')
    else:
        git('worktree', 'add', '--detach', str(directory), 'HEAD')
        git('switch', '--orphan', BRANCH, cwd=directory)


def save(directory: Path):
    directory = directory.resolve()
    if not (directory / '.git').is_file():
        raise SystemExit('상태 디렉터리가 분리된 Git worktree가 아닙니다. 저장을 중단합니다.')
    if not (directory / 'state.json').exists():
        print('저장할 상태 파일이 없습니다.')
        return
    git('config', 'user.name', 'github-actions[bot]', cwd=directory)
    git('config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com', cwd=directory)
    git('add', '--', 'state.json', cwd=directory)
    diff = subprocess.run(['git', 'diff', '--cached', '--quiet'], cwd=directory)
    if diff.returncode == 0:
        print('상태 변경 없음')
        return
    if diff.returncode != 1:
        raise SystemExit('상태 변경 검사 실패')
    git('commit', '-m', 'Update digest state [skip ci]', cwd=directory)
    # Non-force push protects against unanticipated concurrent writers.
    git('push', 'origin', f'HEAD:refs/heads/{BRANCH}', cwd=directory)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['restore', 'save'])
    parser.add_argument('--dir', type=Path, default=Path('state'))
    args = parser.parse_args()
    (restore if args.action == 'restore' else save)(args.dir)
