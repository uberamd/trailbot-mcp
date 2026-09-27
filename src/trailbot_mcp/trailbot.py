"""Fetch and interpret trail data from trailbot.com.

Two sources:
  /trails                                  -> index of every trail (name, city, state, regions,
                                              org) but no status. A Next.js page; we read the
                                              __NEXT_DATA__ JSON it embeds.
  /api/public/organizations/<org>/trails   -> that org's trails with current status. Trailbot's
                                              public (CORS-open) JSON API, the same data its
                                              pages render.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

BASE_URL = "https://trailbot.com"
USER_AGENT = "trailbot-mcp/0.3 (personal trail-status lookup)"

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json"[^>]*>(.*?)</script>', re.S
)


class TrailbotError(Exception):
    pass


def _page_props(html: str, what: str) -> dict:
    m = _NEXT_DATA_RE.search(html)
    if not m:
        raise TrailbotError(f"No __NEXT_DATA__ on the {what} page; Trailbot's page layout may have changed.")
    try:
        return json.loads(m.group(1))["props"]["pageProps"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise TrailbotError(f"Unexpected Trailbot page data for {what}: {e!r}") from e


@dataclass
class IndexTrail:
    """A trail as listed on /trails: where it is and who manages it, but no status."""

    name: str
    slug: str
    org: str
    city: str
    state: str
    regions: list[str]


@dataclass
class Area:
    code: str
    name: str
    regions: dict[str, str]  # region key -> display name


@dataclass
class Index:
    trails: list[IndexTrail]
    areas: dict[str, Area] = field(default_factory=dict)


def parse_index(html: str) -> Index:
    props = _page_props(html, "/trails")
    try:
        trails = [
            IndexTrail(
                name=(t.get("trailName") or t["slug"]).strip(),
                slug=t["slug"],
                org=t["organization"]["slug"],
                city=(t.get("city") or "").strip(),
                state=(t.get("state") or "").strip(),
                regions=list(t.get("regions") or []),
            )
            for t in props["trails"]
        ]
    except (KeyError, TypeError) as e:
        raise TrailbotError(f"Unexpected trail entry in /trails data: {e!r}") from e
    areas = {
        code: Area(code=code, name=a.get("displayName") or code, regions=dict(a.get("regions") or {}))
        for code, a in (props.get("areas") or {}).items()
    }
    # Some trails use a full name ("Ontario") instead of the code ("ON"); normalize.
    by_name = {a.name.lower(): code for code, a in areas.items()}
    for t in trails:
        t.state = by_name.get(t.state.lower(), t.state.upper())
    return Index(trails=trails, areas=areas)


@dataclass
class Trail:
    """A trail with current status, from an org page."""

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
            name=(raw.get("trailName") or raw.get("slug") or "?").strip(),
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

    @property
    def location(self) -> str | None:
        return ", ".join(p for p in (self.city, self.state) if p) or None

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
        if self.open_for_riding is None:
            verdict = (
                "not marked open or closed on Trailbot (see the maintainer note)"
                if self.status == "Unknown"
                else f"marked '{self.status}'"
            )
        else:
            verdict = "OPEN" if self.open_for_riding else "CLOSED"
        where = f" ({self.location})" if self.location else ""
        head = f"{self.name}{where} is {verdict}"
        if self.tags:
            head += f", conditions: {', '.join(self.tags)}"
        parts = [head + "."]
        if ago := self.updated_ago(now):
            parts.append(f"Status last updated {ago} ({self.updated_local()}).")
        if self.note:
            parts.append(f"Maintainer note: {self.note}")
        return " ".join(parts)

    def to_dict(self, now: float | None = None) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "open_for_riding": self.open_for_riding,
            "condition_tags": self.tags,
            "maintainer_note": self.note or None,
            "updated": self.updated_local(),
            "updated_ago": self.updated_ago(now),
            "location": self.location,
            "regions": self.regions,
            "precip_last_24h": self.precip_24h,
            "precip_type": self.precip_type,
            "url": f"{BASE_URL}/trails/{quote(self.org)}/{self.slug}" if self.slug else None,
        }


def parse_org_trails(body: str, org: str) -> list[Trail]:
    try:
        raw_trails = json.loads(body)["trails"]
        return [Trail.from_raw(t, org) for t in raw_trails if t.get("active", True)]
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as e:
        raise TrailbotError(f"Unexpected response from Trailbot's public API for {org}: {e!r}") from e


class _TTLCache:
    """Per-key cache with single-flight fetches and stale fallback on failure."""

    def __init__(self, ttl: float):
        self.ttl = ttl
        self._data: dict[str, tuple[float, object]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def get(self, key: str, fetch) -> tuple[object, bool]:
        hit = self._data.get(key)
        if hit and time.monotonic() - hit[0] < self.ttl:
            return hit[1], False
        async with self._locks.setdefault(key, asyncio.Lock()):
            hit = self._data.get(key)
            if hit and time.monotonic() - hit[0] < self.ttl:
                return hit[1], False
            try:
                value = await fetch()
            except (httpx.HTTPError, TrailbotError):
                if hit:
                    return hit[1], True
                raise
            self._data[key] = (time.monotonic(), value)
            return value, False


class TrailbotClient:
    def __init__(
        self,
        ttl_seconds: float = 300,
        index_ttl_seconds: float = 6 * 3600,
        max_concurrency: int = 4,
        http: httpx.AsyncClient | None = None,
    ):
        self._http = http or httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"User-Agent": USER_AGENT},
            timeout=15,
            follow_redirects=True,
        )
        self.org_cache = _TTLCache(ttl_seconds)
        self.index_cache = _TTLCache(index_ttl_seconds)
        self._sem = asyncio.Semaphore(max_concurrency)

    async def _get(self, path: str) -> str:
        async with self._sem:
            resp = await self._http.get(path)
        if resp.status_code == 404:
            raise TrailbotError(f"Trailbot page not found: {path}")
        resp.raise_for_status()
        return resp.text

    async def index(self) -> tuple[Index, bool]:
        """Return (index, stale)."""

        async def fetch():
            return parse_index(await self._get("/trails"))

        return await self.index_cache.get("index", fetch)

    async def trails(self, org: str) -> tuple[list[Trail], bool]:
        """Return (trails with status, stale) for one org."""

        async def fetch():
            return parse_org_trails(
                await self._get(f"/api/public/organizations/{quote(org)}/trails"), org
            )

        return await self.org_cache.get(org, fetch)
