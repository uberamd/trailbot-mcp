"""Fetch and interpret trail status from trailbot.com.

Trailbot is a Next.js site. Each org page (https://trailbot.com/trails/<org>)
embeds its full page data in a <script id="__NEXT_DATA__"> JSON blob. We read
that instead of /_next/data/<buildId>/... because the buildId changes on every
Trailbot deploy, while the page URL does not.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

BASE_URL = "https://trailbot.com"
USER_AGENT = "trailbot-mcp/0.1 (personal trail-status lookup)"

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>', re.S
)


class TrailbotError(Exception):
    pass


@dataclass
class Trail:
    name: str
    slug: str
    org: str
    status: str  # raw trailStatus, e.g. "Open" / "Closed"
    tags: list[str]
    note: str
    updated_at_ms: int | None
    timezone: str
    city: str | None
    state: str | None
    regions: list[str]
    precip_24h: float | None
    precip_type: str | None

    @classmethod
    def from_raw(cls, raw: dict, org: str) -> "Trail":
        return cls(
            name=raw.get("trailName") or raw.get("slug") or "?",
            slug=raw.get("slug") or "",
            org=org,
            status=(raw.get("trailStatus") or "Unknown").strip(),
            tags=list(raw.get("statusTags") or []),
            note=(raw.get("description") or "").strip(),
            updated_at_ms=raw.get("updatedAt"),
            timezone=raw.get("timezone") or "America/Chicago",
            city=raw.get("city"),
            state=raw.get("state"),
            regions=list(raw.get("regions") or []),
            precip_24h=raw.get("last24Precip"),
            precip_type=raw.get("last24PrecipType"),
        )

    @property
    def open_for_riding(self) -> bool | None:
        s = self.status.lower()
        if s == "open":
            return True
        if s == "closed":
            return False
        return None

    def updated_local(self) -> str | None:
        if not self.updated_at_ms:
            return None
        dt = datetime.fromtimestamp(self.updated_at_ms / 1000, ZoneInfo(self.timezone))
        return dt.strftime("%a %b %-d %Y, %-I:%M %p %Z")

    def updated_ago(self, now: float | None = None) -> str | None:
        if not self.updated_at_ms:
            return None
        secs = max(0, (now or time.time()) - self.updated_at_ms / 1000)
        for unit, size in (("day", 86400), ("hour", 3600), ("minute", 60)):
            if secs >= size:
                n = int(secs // size)
                return f"{n} {unit}{'s' if n != 1 else ''} ago"
        return "just now"

    def summary(self, now: float | None = None) -> str:
        verdict = {True: "OPEN", False: "CLOSED", None: f"status '{self.status}'"}[
            self.open_for_riding
        ]
        head = f"{self.name} is {verdict}"
        if self.tags:
            head += f" (conditions: {', '.join(self.tags)})"
        parts = [head + "."]
        if ago := self.updated_ago(now):
            parts.append(f"Status last updated {ago} ({self.updated_local()}).")
        if self.note:
            parts.append(f"Maintainer note: {self.note}")
        return " ".join(parts)

    def to_dict(self, now: float | None = None) -> dict:
        return {
            "name": self.name,
            "slug": self.slug,
            "org": self.org,
            "status": self.status,
            "open_for_riding": self.open_for_riding,
            "condition_tags": self.tags,
            "maintainer_note": self.note or None,
            "updated": self.updated_local(),
            "updated_ago": self.updated_ago(now),
            "location": ", ".join(p for p in (self.city, self.state) if p) or None,
            "regions": self.regions,
            "precip_last_24h": self.precip_24h,
            "precip_type": self.precip_type,
            "url": f"{BASE_URL}/trails/{self.org}/{self.slug}" if self.slug else None,
        }


def parse_org_page(html: str, org: str) -> list[Trail]:
    m = _NEXT_DATA_RE.search(html)
    if not m:
        raise TrailbotError(f"No __NEXT_DATA__ found on the {org} page; Trailbot's page layout may have changed.")
    try:
        props = json.loads(m.group(1))["props"]["pageProps"]
        raw_trails = props["trails"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise TrailbotError(f"Unexpected Trailbot page data for {org}: {e!r}") from e
    return [Trail.from_raw(t, org) for t in raw_trails if t.get("active", True)]


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def match_trails(query: str, trails: list[Trail]) -> list[Trail]:
    """Best-effort fuzzy lookup: exact > all query words present > close spelling."""
    q = _norm(query)
    if not q:
        return []
    for t in trails:
        if q in (_norm(t.name), _norm(t.slug)):
            return [t]
    words = q.split()
    hits = [t for t in trails if all(w in _norm(t.name) or w in _norm(t.slug) for w in words)]
    if hits:
        return hits
    names = {_norm(t.name): t for t in trails}
    close = difflib.get_close_matches(q, names, n=3, cutoff=0.6)
    if close:
        return [names[c] for c in close]
    # Last resort: match any single query word closely against any single name word
    # (handles "wirth" typos like "worth", or "murphy hanrahan").
    scored = []
    for t in trails:
        name_words = _norm(t.name).split()
        score = sum(
            1 for w in words if len(w) > 3 and difflib.get_close_matches(w, name_words, n=1, cutoff=0.8)
        )
        if score:
            scored.append((score, t))
    if not scored:
        return []
    best = max(s for s, _ in scored)
    return [t for s, t in scored if s == best]


class TrailbotClient:
    def __init__(self, ttl_seconds: float = 300, http: httpx.AsyncClient | None = None):
        self.ttl = ttl_seconds
        self._http = http or httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"User-Agent": USER_AGENT},
            timeout=15,
            follow_redirects=True,
        )
        self._cache: dict[str, tuple[float, list[Trail]]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def trails(self, org: str) -> tuple[list[Trail], bool]:
        """Return (trails, stale). Serves stale cache if Trailbot is unreachable."""
        org = org.strip().lower()
        cached = self._cache.get(org)
        if cached and time.monotonic() - cached[0] < self.ttl:
            return cached[1], False
        async with self._locks.setdefault(org, asyncio.Lock()):
            cached = self._cache.get(org)
            if cached and time.monotonic() - cached[0] < self.ttl:
                return cached[1], False
            try:
                resp = await self._http.get(f"/trails/{org}")
                if resp.status_code == 404:
                    raise TrailbotError(f"Trailbot has no organization '{org}'.")
                resp.raise_for_status()
                trails = parse_org_page(resp.text, org)
            except (httpx.HTTPError, TrailbotError):
                if cached:
                    return cached[1], True
                raise
            self._cache[org] = (time.monotonic(), trails)
            return trails, False
