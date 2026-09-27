"""MCP server (streamable HTTP) exposing Trailbot trail status."""

from __future__ import annotations

import hmac
import os

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse

from .trailbot import Trail, TrailbotClient, TrailbotError, match_trails

DEFAULT_ORGS = [o.strip().lower() for o in os.getenv("TRAILBOT_ORGS", "morc").split(",") if o.strip()]
CACHE_TTL = float(os.getenv("TRAILBOT_CACHE_TTL", "300"))

client = TrailbotClient(ttl_seconds=CACHE_TTL)

mcp = MCPServer(
    name="trailbot",
    instructions=(
        "Mountain bike trail status from trailbot.com (default orgs: "
        + ", ".join(DEFAULT_ORGS)
        + "). To answer 'is <trail> open?', call get_trail_status with the trail name as the user said it. "
        "Relay the status, condition tags, when it was last updated, and any maintainer note. "
        "A trail marked Closed should not be ridden, even if the weather looks fine."
    ),
)


async def _load(orgs: list[str]) -> tuple[list[Trail], list[str]]:
    trails: list[Trail] = []
    warnings: list[str] = []
    for org in orgs:
        try:
            ts, stale = await client.trails(org)
        except Exception as e:  # keep other orgs usable if one fails
            warnings.append(f"Could not load '{org}' from Trailbot: {e}")
            continue
        if stale:
            warnings.append(f"Trailbot unreachable; '{org}' data is from cache and may be out of date.")
        trails.extend(ts)
    return trails, warnings


def _orgs(org: str | None) -> list[str]:
    return [org.strip().lower()] if org and org.strip() else DEFAULT_ORGS


@mcp.tool()
async def get_trail_status(trail: str, org: str | None = None) -> dict:
    """Check whether a mountain bike trail is open for riding right now.

    Args:
        trail: Trail name as the user said it; partial names and small typos are fine
            (e.g. "lebo", "wirth", "murphy").
        org: Optional Trailbot organization slug (e.g. "morc"). Defaults to the configured orgs.
    """
    trails, warnings = await _load(_orgs(org))
    if not trails:
        raise TrailbotError("; ".join(warnings) or "No trails available.")
    matches = match_trails(trail, trails)
    result: dict = {"query": trail}
    if not matches:
        result["summary"] = f"No trail matching '{trail}'. Known trails: " + ", ".join(t.name for t in trails)
        result["matches"] = []
    elif len(matches) == 1:
        result["summary"] = matches[0].summary()
        result["matches"] = [matches[0].to_dict()]
    else:
        result["summary"] = f"'{trail}' matches {len(matches)} trails: " + " | ".join(
            m.summary() for m in matches
        )
        result["matches"] = [m.to_dict() for m in matches]
    if warnings:
        result["warnings"] = warnings
    return result


@mcp.tool()
async def list_trails(org: str | None = None, status: str | None = None) -> dict:
    """List trails with their current open/closed status and conditions.

    Args:
        org: Optional Trailbot organization slug (e.g. "morc"). Defaults to the configured orgs.
        status: Optional filter, "open" or "closed".
    """
    trails, warnings = await _load(_orgs(org))
    if status:
        trails = [t for t in trails if t.status.lower() == status.strip().lower()]
    rows = [
        {
            "name": t.name,
            "status": t.status,
            "open_for_riding": t.open_for_riding,
            "condition_tags": t.tags,
            "updated_ago": t.updated_ago(),
        }
        for t in sorted(trails, key=lambda t: t.name.lower())
    ]
    result: dict = {
        "count": len(rows),
        "open": sum(1 for t in trails if t.open_for_riding),
        "closed": sum(1 for t in trails if t.open_for_riding is False),
        "trails": rows,
    }
    if warnings:
        result["warnings"] = warnings
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
