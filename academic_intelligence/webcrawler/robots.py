"""robots.txt pre-check for the webcrawler (WP3).

Implements the polite-mode robots policy (functional-design.md §6.3):
before any page fetch the crawler asks whether the user agent is allowed to
crawl the URL.  A denial produces a ``blocked`` document — the crawler never
fetches past a robots.txt refusal.

The checker uses :class:`urllib.robotparser.RobotFileParser` per RFC 9309.
``robots.txt`` is fetched once per origin (memoized) with the same
politeness discipline as page fetches.  An unreachable or absent
``robots.txt`` (404) fails *open* with a diagnostic note, matching the
convention that sites without a robots file allow crawling by default;
an explicit ``Disallow`` never fails open.
"""

from __future__ import annotations

import logging
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx
from pydantic import BaseModel

from academic_intelligence.utils.rate_limiter import RateLimiter

from .fetchers import DEFAULT_USER_AGENT

logger = logging.getLogger(__name__)


class RobotsDecision(BaseModel):
    """Outcome of the robots pre-check for one URL.

    Attributes:
        allowed: Whether crawling is permitted by robots.txt.
        reason: Short human-readable explanation.
        source: robots.txt URL that was consulted (or ``"unavailable"``).
        status: HTTP status of the robots.txt fetch, when available.
    """

    allowed: bool = True
    reason: str = ""
    source: str = ""
    status: int | None = None


class RobotsChecker:
    """Per-origin robots.txt policy resolver.

    Two construction modes:

    - Real mode (:meth:`__init__`): fetches ``<origin>/robots.txt`` lazily
      per origin, memoized for the checker lifetime.
    - Fixture mode (:meth:`from_text`): the rules are pre-loaded from a
      robots.txt text (used by offline tests and callers that already hold
      the content); no network is ever touched.
    """

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        rate_limiter: RateLimiter | None = None,
        allow_if_unavailable: bool = True,
    ) -> None:
        """Initialize the checker.

        Args:
            user_agent: User-Agent used for robots fetch and ``can_fetch``.
            timeout: robots.txt fetch timeout in seconds.
            transport: Optional httpx transport (tests inject ``MockTransport``).
            rate_limiter: Optional shared rate limiter (politeness).
            allow_if_unavailable: Fail open (with a note) when robots.txt
                cannot be fetched; ``False`` fails closed to ``blocked``.
        """
        self._user_agent = user_agent
        self._timeout = timeout
        self._transport = transport
        self._rate_limiter = rate_limiter
        self._allow_if_unavailable = allow_if_unavailable
        # ``None`` entries mean "robots.txt unavailable for this origin".
        self._parsers: dict[str, RobotFileParser | None] = {}
        self._decisions: dict[str, RobotsDecision] = {}
        self._client: httpx.AsyncClient | None = None

    @classmethod
    def from_text(cls, text: str, *, user_agent: str = DEFAULT_USER_AGENT) -> RobotsChecker:
        """Build a checker whose rules come from *text* (fixture mode).

        The same rules apply to every origin checked; intended for tests and
        for callers that already have robots.txt content in hand.
        """
        checker = cls(user_agent=user_agent)
        parser = RobotFileParser()
        parser.parse(text.splitlines())
        checker._parsers["*"] = parser
        return checker

    async def check(self, url: str) -> RobotsDecision:
        """Resolve the robots policy for *url*.

        Memoized per URL.  Never raises for robots-related failures; a
        fetch problem is reported through the decision (fail-open by
        default, with ``status``/``reason`` for diagnostics).
        """
        if url in self._decisions:
            return self._decisions[url]

        origin = self._origin(url)
        parser = await self._load(origin)
        if parser is None:
            if not self._allow_if_unavailable:
                decision = RobotsDecision(
                    allowed=False,
                    reason="robots.txt could not be fetched (fail-closed mode)",
                    source="unavailable",
                )
            else:
                decision = RobotsDecision(
                    allowed=True,
                    reason="robots.txt unavailable; proceeding (fail-open)",
                    source="unavailable",
                )
        else:
            can_fetch = parser.can_fetch(self._user_agent, url)
            decision = RobotsDecision(
                allowed=can_fetch,
                reason=(
                    "robots.txt permits crawling"
                    if can_fetch
                    else "robots.txt disallows crawling this URL"
                ),
                source=f"{origin}/robots.txt",
                status=200,
            )
        self._decisions[url] = decision
        return decision

    @staticmethod
    def _origin(url: str) -> str:
        parsed = urlparse(url)
        return f"{parsed.scheme}://{parsed.netloc}"

    async def _load(self, origin: str) -> RobotFileParser | None:
        if origin in self._parsers:
            return self._parsers[origin]
        # Fixture mode (from_text): one preloaded rule set applies to every
        # origin; no robots.txt fetch is ever performed.
        if "*" in self._parsers:
            return self._parsers["*"]
        parser: RobotFileParser | None = await self._fetch_parser(origin)
        self._parsers[origin] = parser
        return parser

    async def _fetch_parser(self, origin: str) -> RobotFileParser | None:
        robots_url = urljoin(origin + "/", "robots.txt")
        if self._rate_limiter is not None:
            await self._rate_limiter.acquire()
        try:
            client = await self._ensure_client()
            response = await client.get(robots_url)
        except httpx.RequestError as exc:
            logger.info("robots.txt fetch failed for %s: %s", origin, exc)
            return None
        if response.status_code >= 400:
            # 404 (and other error codes): no policy → allow (or fail closed
            # per configuration) — never treat as a denial.
            return None
        parser = RobotFileParser()
        parser.parse(response.text.splitlines())
        return parser

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=True,
                transport=self._transport,
                headers={"User-Agent": self._user_agent},
            )
        return self._client

    async def close(self) -> None:
        """Release the underlying httpx client, if any."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
