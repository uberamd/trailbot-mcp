"""MCP server (streamable HTTP) exposing Trailbot trail status."""

from __future__ import annotations

import asyncio
import hmac
import os

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from .lookup import ALL_WORDS, match_trails_scored, resolve_location, split_trail_and_place
from .trailbot import Index, IndexTrail, Trail, TrailbotClient, TrailbotError

HOME = os.getenv("TRAILBOT_HOME", "").strip() or None  # e.g. "Minnesota" or "Twin Cities Metro"
CACHE_TTL = float(os.getenv("TRAILBOT_CACHE_TTL", "300"))
MAX_ORGS = int(os.getenv("TRAILBOT_MAX_ORGS_PER_QUERY", "12"))
MAX_MATCHES = 8

client = TrailbotClient(ttl_seconds=CACHE_TTL)

mcp = MCPServer(
    name="trailbot",
    instructions=(
        "Live mountain bike trail status (open/closed, conditions, maintainer notes) from trailbot.com, "
        "which covers ~330 trails mostly in the US Midwest."
        + (f" The user's home area is {HOME}." if HOME else "")
        + " For 'is <trail> open?' call get_trail_status; pass any place the user mentions as `location`. "
        "For 'what's open near <place>?' call list_trails. Relay the status, conditions, how recently it was "
        "updated, and the maintainer note. A Closed trail should not be ridden, even if the weather looks fine. "
        "If several trails match, ask the user which one they meant."
    ),
)


class _Warnings(list):
    def stale(self, what: str) -> None:
        self.append(f"Trailbot unreachable; {what} is from cache and may be out of date.")


async def _index(warnings: _Warnings) -> Index:
    index, stale = await client.index()
    if stale:
        warnings.stale("the trail list")
    return index


async def _statuses(orgs: set[str], warnings: _Warnings) -> dict[tuple[str, str], Trail]:
    """Fetch org pages concurrently; return {(org, slug): Trail}."""
    results = await asyncio.gather(*(client.trails(o) for o in sorted(orgs)), return_exceptions=True)
    out: dict[tuple[str, str], Trail] = {}
    for org, res in zip(sorted(orgs), results):
        if isinstance(res, BaseException):
            warnings.append(f"Could not load status for trails managed by '{org}': {res}")
            continue
        trails, stale = res
        if stale:
            warnings.stale(f"status for '{org}' trails")
        out.update({(org, t.slug): t for t in trails})
    return out


def _unavailable(t: IndexTrail) -> dict:
    return {
        "name": t.name,
        "status": "Unknown",
        "open_for_riding": None,
        "location": ", ".join(p for p in (t.city, t.state) if p),
        "summary": f"{t.name} ({t.city}, {t.state}): current status unavailable.",
    }


def _place_help(text: str) -> str:
    return (
        f"Couldn't find a place called '{text}'. Try a state or province (e.g. 'Minnesota'), a region "
        "(e.g. 'Twin Cities Metro', 'Duluth'), or a city."
    )


@mcp.tool()
async def get_trail_status(trail: str, location: str | None = None) -> dict:
    """Is a mountain bike trail open for riding right now?

    Args:
        trail: The trail name as the user said it. Partial names and typos are fine
            ("lebo", "theo wirth", "murphy hanrahan"). It may include a place too,
            e.g. "hillside in minnesota".
        location: Optional place to narrow the search: a state/province ("Minnesota", "MN"),
            region ("Twin Cities", "Duluth", "Southeast MN") or city ("Elk River").
    """
    warnings = _Warnings()
    index = await _index(warnings)
    scope = None
    pool = index.trails

    if location:
        found = resolve_location(location, index)
        if not found:
            return {"summary": _place_help(location), "matches": []}
        scope, pool = found
    elif split := split_trail_and_place(trail, index):
        trail, scope, pool = split

    strength, matches = match_trails_scored(trail, pool)
    note = None
    if scope and strength < ALL_WORDS:
        # Nothing convincing in that place; a clear match elsewhere beats a guess.
        g_strength, g_matches = match_trails_scored(trail, index.trails)
        if g_matches and (not matches or g_strength >= ALL_WORDS):
            matches = g_matches
            note = f"No trail matching '{trail}' in {scope}; showing matches elsewhere."
    elif not scope and HOME and len(matches) > 1:
        # Bare query with several hits: prefer ones in the user's home area.
        home = resolve_location(HOME, index)
        local = [m for m in matches if home and m in home[1]]
        if local:
            matches = local

    if not matches:
        where = f" in {scope}" if scope else ""
        return {"summary": f"Trailbot has no trail matching '{trail}'{where}.", "matches": []}

    truncated = len(matches) > MAX_MATCHES
    matches = matches[:MAX_MATCHES]
    status = await _statuses({m.org for m in matches}, warnings)

    rows = []
    for m in matches:
        t = status.get((m.org, m.slug))
        rows.append({**t.to_dict(), "summary": t.summary()} if t else _unavailable(m))

    if len(rows) == 1:
        summary = rows[0]["summary"]
    else:
        summary = f"{len(rows)}{'+' if truncated else ''} trails match '{trail}'. Ask which one if unclear. " + " | ".join(
            f"{r['name']} ({r['location']}): {r['status']}" for r in rows
        )
    result: dict = {"summary": (note + " " if note else "") + summary, "matches": rows}
    if warnings:
        result["warnings"] = list(warnings)
    return result


@mcp.tool()
async def list_trails(location: str | None = None, status: str | None = None) -> dict:
    """What trails are open (or closed) in an area?

    Args:
        location: A state/province, region ("Twin Cities", "Duluth", "Southeast MN") or city.
            Defaults to the user's home area if configured.
        status: Optional filter: "open" or "closed".
    """
    warnings = _Warnings()
    place = location or HOME
    if not place:
        return {"summary": "Which area? Give a state, region (e.g. 'Twin Cities Metro') or city."}
    index = await _index(warnings)
    found = resolve_location(place, index)
    if not found:
        return {"summary": _place_help(place)}
    label, pool = found

    orgs = {t.org for t in pool}
    if len(orgs) > MAX_ORGS:
        states = {t.state for t in pool}
        regions = sorted(
            {d for s in states if s in index.areas for k, d in index.areas[s].regions.items() if not k.endswith("-All")}
        )
        return {
            "summary": (
                f"{label} has {len(pool)} trails, too many to check at once. Pick a region: "
                + ", ".join(regions)
                + ". Or ask about a specific trail."
            ),
            "regions": regions,
        }

    by_key = await _statuses(orgs, warnings)
    trails = [by_key[(t.org, t.slug)] for t in pool if (t.org, t.slug) in by_key]
    missing = [t.name for t in pool if (t.org, t.slug) not in by_key]
    if status:
        trails = [t for t in trails if t.status.lower() == status.strip().lower()]
    trails.sort(key=lambda t: t.name.lower())

    def line(t: Trail) -> str:
        return t.name + (f" ({', '.join(t.tags)})" if t.tags else "")

    open_ = [line(t) for t in trails if t.open_for_riding]
    closed = [line(t) for t in trails if t.open_for_riding is False]
    other = [f"{t.name} ({t.status})" for t in trails if t.open_for_riding is None]
    parts = [f"{label}:"]
    if open_:
        parts.append(f"OPEN ({len(open_)}): " + "; ".join(open_) + ".")
    if closed:
        parts.append(f"CLOSED ({len(closed)}): " + "; ".join(closed) + ".")
    if other:
        parts.append("OTHER: " + "; ".join(other) + ".")
    if not trails:
        parts.append("no trails" + (f" with status '{status}'." if status else "."))

    result: dict = {
        "summary": " ".join(parts),
        "trails": [
            {
                "name": t.name,
                "location": t.location,
                "status": t.status,
                "open_for_riding": t.open_for_riding,
                "condition_tags": t.tags,
                "updated_ago": t.updated_ago(),
            }
            for t in trails
        ],
    }
    if missing:
        warnings.append("Status unavailable for: " + ", ".join(missing))
    if warnings:
        result["warnings"] = list(warnings)
    return result


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


class BearerAuth:
    """Require `Authorization: Bearer <token>` on everything except /healthz."""

    def __init__(self, app, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"] != "/healthz":
            got = dict(scope["headers"]).get(b"authorization", b"")
            if not hmac.compare_digest(got, self.expected):
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def build_app():
    allowed_hosts = [h.strip() for h in os.getenv("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=bool(allowed_hosts),
        allowed_hosts=allowed_hosts,
    )
    app = mcp.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=security,
        host="0.0.0.0",
    )
    if token := os.getenv("MCP_AUTH_TOKEN"):
        return BearerAuth(app, token)
    return app


def main():
    uvicorn.run(
        build_app(),
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8000")),
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
