"""Resolve explicit KST publication targets and wait on the same runner."""
from __future__ import annotations

import argparse
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .common import CRON_SLOTS, CRON_TIMEZONES, KST, SLOTS


def aware_date(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Publication timestamps must include a timezone')
    return parsed.astimezone(KST)


def publication_target(created_at: datetime, event: str, cron: str = '',
                       slot: str = 'manual', *, requested_at: datetime | None = None) -> datetime | None:
    if created_at.tzinfo is None:
        raise ValueError('Run creation time must include a timezone')
    anchor = created_at.astimezone(KST)
    if event == 'schedule':
        if cron not in CRON_SLOTS:
            raise ValueError('Unknown scheduled publication slot')
        slot = CRON_SLOTS[cron]
        minute, hour = map(int, cron.split()[:2])
        cron_zone = KST if CRON_TIMEZONES[cron] == 'Asia/Seoul' else timezone.utc
        prepared = anchor.astimezone(cron_zone).replace(
            hour=hour, minute=minute, second=0, microsecond=0)
        if prepared > anchor:
            prepared -= timedelta(days=1)
        prepared = prepared.astimezone(KST)
        target = prepared.replace(hour=int(slot[:2]), minute=0)
        if target < prepared:
            target += timedelta(days=1)
        if requested_at is None:
            return target
    if requested_at is not None:
        if slot not in SLOTS:
            raise ValueError('An explicit publication timestamp needs a scheduled slot')
        if requested_at.tzinfo is None:
            raise ValueError('Publication timestamps must include a timezone')
        target = requested_at.astimezone(KST)
        if target.strftime('%H:%M') != slot or target.second or target.microsecond:
            raise ValueError('Publication timestamp does not match its KST slot')
        if (target - anchor).total_seconds() > 3600:
            raise ValueError(f'Too early for {slot} KST publication; dispatch within one hour of the slot')
        return target
    if slot in ('', 'manual'):
        return None
    if slot not in SLOTS:
        raise ValueError('Unknown scheduled publication slot')
    target = anchor.replace(hour=int(slot[:2]), minute=0, second=0, microsecond=0)
    if (target - anchor).total_seconds() > 3600:
        raise ValueError(f'Too early for {slot} KST publication; dispatch within one hour of the slot')
    return target


def wait_seconds(target: datetime, now: datetime) -> float:
    if target.tzinfo is None or now.tzinfo is None:
        raise ValueError('Publication timestamps must include a timezone')
    seconds = (target - now).total_seconds()
    if seconds > 3600:
        raise ValueError('Publication target is more than one hour ahead')
    return max(0.0, seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('phase', choices=('plan', 'wait'))
    args = parser.parse_args(argv)
    if args.phase == 'plan':
        target = publication_target(aware_date(os.environ['RUN_CREATED_AT']),
                                    os.environ['GITHUB_EVENT_NAME'],
                                    os.getenv('SCHEDULE_CRON', ''),
                                    os.getenv('SCHEDULED_SLOT', 'manual'),
                                    requested_at=aware_date(os.environ['REQUESTED_PUBLISH_AT'])
                                        if os.getenv('REQUESTED_PUBLISH_AT') else None)
        value = target.isoformat() if target else ''
        if os.getenv('GITHUB_OUTPUT'):
            with Path(os.environ['GITHUB_OUTPUT']).open('a', encoding='utf-8') as output:
                output.write(f'publish_at={value}\n')
        print(f'Publication target: {value or "immediate (manual)"}', flush=True)
    else:
        target = aware_date(os.environ['PUBLISH_AT'])
        seconds = wait_seconds(target, datetime.now(KST))
        if seconds:
            print(f'Waiting {seconds:.0f}s until {target.isoformat()} to publish', flush=True)
            time.sleep(seconds)
        else:
            print(f'Publication target {target.isoformat()} passed; deploying immediately', flush=True)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, KeyError) as exc:
        raise SystemExit(str(exc)) from None
