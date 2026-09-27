# trailbot-mcp

A small MCP server (streamable HTTP) that answers "is <trail> open for riding?" from
[Trailbot](https://trailbot.com/trails) data.

## How it gets data

It fetches `https://trailbot.com/trails/<org>` and reads the `__NEXT_DATA__` JSON that
Next.js embeds in the page. That page URL is stable. The `/_next/data/<buildId>/...json`
endpoint returns the same data, but its build ID changes on every Trailbot deploy and old
IDs return 404.

Results are cached in memory per org (5 minutes by default). If Trailbot can't be reached,
the last good data is returned with a warning that it may be out of date.

## Tools

| Tool | Purpose |
|---|---|
| `get_trail_status(trail, org?)` | Fuzzy trail lookup ("lebo", "wirth", "murphy hanrahan"). Returns an OPEN/CLOSED summary, condition tags, the last update time, the maintainer note and 24h precipitation. |
| `list_trails(org?, status?)` | Every trail with its status, optionally filtered to `open` or `closed`. |

## Configuration

| Env var | Default | |
|---|---|---|
| `TRAILBOT_ORGS` | `morc` | Comma-separated org slugs to search (the slug from `trailbot.com/trails/<slug>`). |
| `MCP_AUTH_TOKEN` | unset | If set, every request except `/healthz` needs `Authorization: Bearer <token>`. **Set this when the server is reachable from the internet.** |
| `MCP_ALLOWED_HOSTS` | unset | Optional Host-header allowlist (DNS-rebinding protection), e.g. `trailbot-mcp.example.com`. |
| `TRAILBOT_CACHE_TTL` | `300` | Cache lifetime in seconds. |
| `PORT` | `8000` | |

Endpoints: `POST /mcp` (MCP, stateless, JSON responses) and `GET /healthz`.

## Run

```bash
docker build -t trailbot-mcp .
docker run -p 8000:8000 -e MCP_AUTH_TOKEN=$(openssl rand -hex 24) trailbot-mcp
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

Then ask: *"Is Lebanon Hills open for riding?"*
