"""Dispatch the prepared news workflow from an external clock, with local deduplication.

Examples (gh must already be authenticated):
    python scripts/dispatch_news.py --slot 13:00 --dry-run
    python scripts/dispatch_news.py --slot 13:00
    python scripts/dispatch_news.py --slot 13:00 --publish-at 2026-10-01T13:00:00+09:00

Schedule at 05:07 / 12:07 / 18:07 KST for the 06:00 / 13:00 / 19:00 targets.
An always-on host is needed for reliable timing. A sleeping/offline local PC may
catch up later; this tool cannot guarantee GitHub runner or Pages availability.
Successful/uncertain dispatch receipts are stored only in the ignored state/
directory. If an outcome is uncertain, subsequent calls check recent workflow
run titles for the exact publication timestamp and never blindly dispatch again.
The workflow run-name must include inputs.publish_at for that reconciliation.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
KST = timezone(timedelta(hours=9))
SLOTS = ('06:00', '13:00', '19:00')
PREPARATION_LEAD = timedelta(minutes=53)
MAX_CATCHUP = timedelta(days=1)
MAX_FUTURE = timedelta(hours=1)
DEFAULT_REPOSITORY = 'kiukmaster/kiuk_news'
WORKFLOW = 'update-news.yml'
DEFAULT_RECEIPTS = ROOT / 'state' / 'local-scheduler'


class DispatchError(RuntimeError):
    """Safe, non-secret diagnostic for a scheduler failure."""


class UncertainDispatch(DispatchError):
    """A previous mutating request may have reached GitHub."""


def publication_target(now: datetime, slot: str, publish_at: str | None = None) -> datetime:
    """Freeze the latest due slot date, including the previous day on a late wake."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError('The scheduler clock must include a timezone')
    if slot not in SLOTS:
        raise ValueError('Unknown publication slot')
    now = now.astimezone(KST)
    if publish_at:
        try:
            target = datetime.fromisoformat(publish_at.replace('Z', '+00:00'))
        except ValueError:
            raise ValueError('publish-at must be a full ISO timestamp with timezone') from None
        if target.tzinfo is None or target.utcoffset() is None:
            raise ValueError('publish-at must include a timezone')
        target = target.astimezone(KST)
        if target.strftime('%H:%M') != slot or target.second or target.microsecond:
            raise ValueError('publish-at must match the exact KST publication slot')
    else:
        target = now.replace(hour=int(slot[:2]), minute=0, second=0, microsecond=0)
        if now < target - PREPARATION_LEAD:
            target -= timedelta(days=1)
    if now - target >= MAX_CATCHUP:
        raise ValueError('Missed publication target is at least one day old; refusing catch-up')
    if target - now > MAX_FUTURE:
        raise ValueError('Publication target is more than one hour in the future')
    return target


def validate_repository(repository: str) -> None:
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository):
        raise ValueError('Repository must be an owner/name identifier')


def dispatch_command(repository: str, slot: str, target: datetime) -> list[str]:
    return ['workflow', 'run', WORKFLOW, '--repo', repository, '--ref', 'main',
            '-f', 'mode=collect', '-f', f'scheduled_slot={slot}',
            '-f', f'publish_at={target.isoformat()}']


def run_gh(arguments: list[str]) -> str:
    """Use the user's existing gh login; do not print environment or CLI output."""
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0) if os.name == 'nt' else 0
    try:
        result = subprocess.run(['gh', *arguments], cwd=ROOT, capture_output=True,
                                text=True, encoding='utf-8', timeout=60,
                                creationflags=flags)
    except subprocess.TimeoutExpired:
        raise DispatchError('gh request timed out; check GitHub Actions before retrying') from None
    except OSError:
        raise DispatchError('Could not start gh; install GitHub CLI and check gh auth status') from None
    if result.returncode:
        # stderr can contain server-provided data; retain only a safe exit code.
        raise DispatchError(f'gh request failed (exit {result.returncode}); check gh auth status and Actions')
    return result.stdout


def recent_matching_run(repository: str, target: datetime, call_gh: Callable[[list[str]], str]) -> dict | None:
    endpoint = f'repos/{repository}/actions/workflows/{WORKFLOW}/runs?event=workflow_dispatch&per_page=100'
    try:
        payload = json.loads(call_gh(['api', endpoint]))
        runs = payload['workflow_runs']
    except (json.JSONDecodeError, KeyError, TypeError):
        raise DispatchError('Could not read the recent workflow run list') from None
    if not isinstance(runs, list):
        raise DispatchError('Unexpected workflow run list format')
    timestamp = target.isoformat()
    for run in runs:
        if (run.get('event') == 'workflow_dispatch' and run.get('head_branch') == 'main'
                and timestamp in str(run.get('display_title', ''))):
            return run
    return None


def write_receipt(path: Path, receipt: dict) -> None:
    """Replace a receipt atomically, including the pre-request uncertainty marker."""
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(receipt, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


@contextmanager
def receipt_lock(directory: Path, key: str):
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / (key + '.lock')
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise DispatchError('Another dispatch holds this slot lock; check the active task or stale lock') from None
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump({'pid': os.getpid(), 'created_at': datetime.now(timezone.utc).isoformat()}, stream)
        yield
    finally:
        lock.unlink(missing_ok=True)


def dispatch(slot: str, *, publish_at: str | None = None, now: datetime | None = None,
             repository: str = DEFAULT_REPOSITORY, receipt_dir: Path = DEFAULT_RECEIPTS,
             dry_run: bool = False, call_gh: Callable[[list[str]], str] = run_gh) -> dict:
    now = now or datetime.now(KST)
    validate_repository(repository)
    target = publication_target(now, slot, publish_at)
    command = dispatch_command(repository, slot, target)
    plan = {'repository': repository, 'slot': slot, 'publish_at': target.isoformat(),
            'prepare_at': (target - PREPARATION_LEAD).isoformat(),
            'command': shlex.join(['gh', *command])}
    if dry_run:
        return {'action': 'dry-run', **plan}
    key = target.strftime('%Y-%m-%d') + '-' + slot.replace(':', '')
    directory = Path(receipt_dir) / repository.replace('/', '--') / WORKFLOW
    receipt_path = directory / (key + '.json')
    with receipt_lock(directory, key):
        previous = None
        if receipt_path.exists():
            try:
                previous = json.loads(receipt_path.read_text(encoding='utf-8'))
            except (json.JSONDecodeError, OSError):
                raise DispatchError('Slot receipt is unreadable; inspect it before retrying') from None
            if (previous.get('publish_at') != target.isoformat()
                    or previous.get('repository') != repository):
                raise DispatchError('Slot receipt does not match this dispatch target')
            if previous.get('status') == 'accepted':
                return {'action': 'already-dispatched', 'receipt': str(receipt_path), **plan}
            if previous.get('status') != 'uncertain':
                raise DispatchError('Unknown slot receipt status; inspect it before retrying')
        # Check for requests from another host and reconcile uncertain responses.
        match = recent_matching_run(repository, target, call_gh)
        receipt = {'repository': repository, 'slot': slot, 'publish_at': target.isoformat(),
                   'attempted_at': now.astimezone(KST).isoformat()}
        if match:
            receipt.update({'status': 'accepted', 'run_id': match.get('id'),
                            'run_url': match.get('html_url'), 'reconciled': True})
            write_receipt(receipt_path, receipt)
            return {'action': 'already-dispatched', 'run_id': match.get('id'),
                    'receipt': str(receipt_path), **plan}
        if previous:
            raise UncertainDispatch('Previous dispatch outcome is uncertain and no matching run is visible. '
                                    'No duplicate request was sent; inspect Actions before clearing the receipt.')
        # A process crash or HTTP timeout from this point may still have dispatched.
        receipt['status'] = 'uncertain'
        write_receipt(receipt_path, receipt)
        call_gh(command)
        receipt['status'] = 'accepted'
        write_receipt(receipt_path, receipt)
        return {'action': 'dispatched', 'receipt': str(receipt_path), **plan}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--slot', choices=SLOTS, required=True)
    parser.add_argument('--publish-at', help='Explicit recovery target, full ISO timestamp with timezone')
    parser.add_argument('--repo', default=DEFAULT_REPOSITORY)
    parser.add_argument('--receipt-dir', type=Path, default=DEFAULT_RECEIPTS)
    parser.add_argument('--dry-run', action='store_true', help='Print the plan without GitHub requests or receipt writes')
    args = parser.parse_args(argv)
    try:
        result = dispatch(args.slot, publish_at=args.publish_at, repository=args.repo,
                          receipt_dir=args.receipt_dir, dry_run=args.dry_run)
    except (ValueError, DispatchError) as error:
        parser.exit(1, str(error) + '\n')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
