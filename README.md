# trailbot-mcp

[![Build Status](https://build.mnlab.cloud/api/badges/uberamd/trailbot-mcp/status.svg)](https://build.mnlab.cloud/uberamd/trailbot-mcp)

A small MCP server (streamable HTTP) that answers "is <trail> open for riding?" from
[Trailbot](https://trailbot.com/trails) data.

## How it gets data

- **Status** comes from Trailbot's public JSON API,
  `https://trailbot.com/api/public/organizations/<org>/trails`, which returns each of that org's
  trails with its status, condition tags, maintainer note and update time. It's the same data
  Trailbot's pages show, and it's served with `Access-Control-Allow-Origin: *` for embedding
  on other sites. Cached for 5 minutes per org.
- **The trail index** (every trail's name, city, state, region and org, but no status) has no
  public API. It comes from the `__NEXT_DATA__` JSON embedded in `https://trailbot.com/trails`.
  Cached for 6 hours.

The API is undocumented, so Trailbot could change it without notice. If either source
changes shape, the tools return a clear error rather than wrong statuses. If Trailbot can't be
reached, cached data is returned with a warning.

## Tools

Both tools take plain English. A place can be a state or province ("Minnesota", "MN"), a region
("Twin Cities", "Duluth", "Southeast Minnesota") or a city ("Elk River", "St Paul").

| Tool | Answers |
|---|---|
| `get_trail_status(trail, location?)` | "Is Hillside in Minnesota open?", "is lebo open?", "Lester in Duluth". Trail names are fuzzy-matched, and a place can be written into the trail text. |
| `list_trails(location?, status?)` | "What's open near Duluth?", "what's closed in the Twin Cities?" Queries covering more than 12 orgs (a whole state) are refused, and the answer suggests regions instead. |

Each answer starts with a one-line `summary` the agent can relay directly, followed by the
structured detail: status, condition tags ("tacky", "wet"), time since the last update,
the maintainer note and 24h precipitation.

## Configuration

| Env var | Default | |
|---|---|---|
| `TRAILBOT_HOME` | unset | Your home area, e.g. `Twin Cities Metro` or `Minnesota`. `list_trails` uses it when no place is given, and it breaks ties for ambiguous trail names. |
| `MCP_AUTH_TOKEN` | unset | If set, every request except `/healthz` needs `Authorization: Bearer <token>`. **Set this when the server is reachable from the internet.** |
| `MCP_ALLOWED_HOSTS` | unset | Optional Host-header allowlist (DNS-rebinding protection), e.g. `trailbot-mcp.example.com`. |
| `TRAILBOT_CACHE_TTL` | `300` | Lifetime of cached trail status, in seconds. |
| `TRAILBOT_MAX_ORGS_PER_QUERY` | `12` | Maximum org pages `list_trails` fetches for one question. |
| `PORT` | `8000` | |

Endpoints: `POST /mcp` (MCP, stateless, JSON responses) and `GET /healthz`.

## Run

```bash
docker build -t trailbot-mcp .
docker run -p 8000:8000 -e MCP_AUTH_TOKEN=$(openssl rand -hex 24) -e TRAILBOT_HOME="Twin Cities Metro" trailbot-mcp
```

Local development:

```bash
uv sync && uv run pytest && uv run trailbot-mcp
```

## Hermes

Point Hermes at the deployed URL. In `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  trailbot:
    url: "https://<your-host>/mcp"
    headers:
      Authorization: "Bearer <MCP_AUTH_TOKEN>"
```

Then ask: *"Is Hillside in Minnesota open?"* or *"What's open around Duluth?"*
