from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

KST = ZoneInfo('Asia/Seoul')
ROOT = Path(__file__).resolve().parents[1]
CATEGORIES = {'ai': 'AI', 'security': '보안', 'tech': '신기술·논문', 'event': '대회·행사', 'github': 'GitHub 인기'}
SLOTS = ('06:00', '13:00', '19:00')
# Each cron starts 53 minutes before the publication slot. GitHub Actions
# publishes the prepared artifact no earlier than the matching UTC hour.
CRON_SLOTS = {'7 20 * * *': '06:00', '7 3 * * *': '13:00', '7 9 * * *': '19:00'}


def now_kst() -> datetime:
    return datetime.now(KST)


def parse_date(value: str | None, default_tz=timezone.utc) -> datetime | None:
    if not value:
        return None
    value = str(value).strip()
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
        except (ValueError, TypeError, OverflowError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=default_tz or timezone.utc)
    return dt.astimezone(KST)


def canonical_url(url: str) -> str:
    """Remove tracking only; retain article identifiers such as idxno and code."""
    p = urlsplit(str(url).strip())
    if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
        raise ValueError('허용되지 않는 URL')
    if p.port not in (None, 80, 443):
        raise ValueError('비표준 포트 URL 제외')
    host = p.hostname.lower().encode('idna').decode('ascii')
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith('utm_')
             and k.lower() not in {'fbclid', 'gclid', 'mc_cid', 'mc_eid', 'ref_src'}]
    return urlunsplit((p.scheme.lower(), host, p.path or '/', urlencode(sorted(query)), ''))


def item_id(url: str) -> str:
    # Treat http/https variants as the same article, without changing fetch URLs.
    p = urlsplit(canonical_url(url))
    key = urlunsplit(('', p.netloc.removeprefix('www.'), p.path.rstrip('/'), p.query, ''))
    return hashlib.sha256(key.encode()).hexdigest()[:24]


def text_key(title: str) -> str:
    return re.sub(r'\W+', '', title.casefold())


def read_json(path: Path, default):
    if not path.exists():
        return default
    # Corrupt persistent state is fatal: never silently overwrite it as empty.
    return json.loads(path.read_text(encoding='utf-8'))


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.replace(path)


def retention_cutoff(now: datetime, keep: int = 5) -> str:
    return (now.astimezone(KST).date() - timedelta(days=keep - 1)).isoformat()


def load_config() -> dict:
    return read_json(ROOT / 'config/settings.json', {})
