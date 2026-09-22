from __future__ import annotations

import ipaddress
import socket
import time
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import requests

from .common import canonical_url


class FetchError(RuntimeError):
    pass


class PublicWeb:
    """Bounded, unauthenticated public fetches. No bypass of robots or paywalls."""

    def __init__(self, user_agent: str, timeout: int = 18, delay: float = 0.7):
        self.user_agent = user_agent
        self.timeout = timeout
        self.delay = delay
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({'User-Agent': user_agent, 'Accept-Language': 'ko,en;q=0.8'})
        self.robots: dict[str, RobotFileParser | bool] = {}
        self.last_access: dict[str, float] = {}
        self.robot_errors: dict[str, str] = {}

    @staticmethod
    def validate(url: str, allowed_hosts: list[str]) -> str:
        url = canonical_url(url)
        host = urlsplit(url).hostname or ''
        if not any(host == x or host.endswith('.' + x) for x in allowed_hosts):
            raise FetchError('설정한 수집 도메인 밖의 URL 차단')
        try:
            addresses = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except OSError as e:
            raise FetchError('DNS 조회 실패') from e
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise FetchError('공개 인터넷 주소가 아닌 URL 차단')
        return url

    def _throttle(self, host: str, delay: float | None = None) -> None:
        pause = (self.delay if delay is None else max(delay, self.delay))
        remaining = self.last_access.get(host, 0) + pause - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        self.last_access[host] = time.monotonic()

    def _get(self, url: str, allowed_hosts: list[str], max_bytes: int, check_robots: bool):
        for _ in range(6):
            url = self.validate(url, allowed_hosts)
            host = urlsplit(url).hostname or ''
            crawl_delay = None
            if check_robots:
                policy = self._policy(url, allowed_hosts)
                if policy is False:
                    origin = f'{urlsplit(url).scheme}://{urlsplit(url).netloc}'
                    reason = self.robot_errors.get(origin, '정책을 확인할 수 없음')
                    raise FetchError(f'robots.txt 확인 실패: {origin} — {reason}')
                if not isinstance(policy, bool) and not policy.can_fetch(self.user_agent, url):
                    raise FetchError(f'robots.txt 규칙에 따른 수집 금지: {host}{urlsplit(url).path}')
                if not isinstance(policy, bool):
                    crawl_delay = policy.crawl_delay(self.user_agent) or policy.crawl_delay('*')
                    if crawl_delay and crawl_delay > 60:
                        raise FetchError('긴 robots crawl-delay로 이번 수집 보류')
            self._throttle(host, crawl_delay)
            try:
                response = self.session.get(url, timeout=(8, self.timeout), stream=True, allow_redirects=False)
            except requests.RequestException as e:
                raise FetchError(f'네트워크 요청 실패: {type(e).__name__}') from e
            if response.is_redirect:
                target = urljoin(url, response.headers.get('Location', ''))
                response.close()
                url = target
                continue
            with response:
                if response.status_code >= 400:
                    error = FetchError(f'HTTP {response.status_code}')
                    error.status_code = response.status_code
                    raise error
                parts, count = [], 0
                started = time.monotonic()
                for block in response.iter_content(32768):
                    count += len(block)
                    if count > max_bytes or time.monotonic() - started > 45:
                        raise FetchError('응답 크기 또는 수신 시간 제한 초과')
                    parts.append(block)
                return b''.join(parts), response.headers.get('Content-Type', ''), url
        raise FetchError('리디렉션 횟수 초과')

    def _policy(self, url: str, allowed_hosts: list[str]):
        p = urlsplit(url)
        origin = f'{p.scheme}://{p.netloc}'
        if origin not in self.robots:
            try:
                body, _, _ = self._get(origin + '/robots.txt', allowed_hosts, 512_000, False)
                text = body.decode('utf-8', errors='replace')
                # An HTML error/landing page is not a valid robots response.
                if '<html' in text[:1000].lower():
                    self.robots[origin] = False
                    self.robot_errors[origin] = 'robots 응답이 HTML 페이지임'
                else:
                    robot = RobotFileParser()
                    robot.parse(text.splitlines())
                    self.robots[origin] = robot
            except FetchError as e:
                # 404/410 mean robots.txt is absent; inaccessible policy is fail-closed.
                self.robots[origin] = getattr(e, 'status_code', None) in (404, 410)
                if not self.robots[origin]:
                    self.robot_errors[origin] = str(e)
        return self.robots[origin]

    def get(self, url: str, allowed_hosts: list[str], max_bytes: int = 6_000_000):
        return self._get(url, allowed_hosts, max_bytes, True)
