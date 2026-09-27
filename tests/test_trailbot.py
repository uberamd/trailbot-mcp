from pathlib import Path

import httpx
import pytest

from trailbot_mcp import server
from trailbot_mcp.trailbot import TrailbotClient, TrailbotError, match_trails, parse_org_page

HTML = (Path(__file__).parent / "fixtures" / "morc.html").read_text()
TRAILS = parse_org_page(HTML, "morc")


def names(q):
    return [t.name for t in match_trails(q, TRAILS)]


def test_parse_fixture():
    assert len(TRAILS) == 19
    leb = next(t for t in TRAILS if t.slug == "lebanon-hills")
    assert leb.open_for_riding is True
    rebecca = next(t for t in TRAILS if t.slug == "lake-rebecca")
    assert rebecca.open_for_riding is False
    assert rebecca.tags == ["greasy", "wet"]


def test_parse_rejects_non_next_page():
    with pytest.raises(TrailbotError):
        parse_org_page("<html>nope</html>", "morc")


@pytest.mark.parametrize(
    "query,expected",
    [
        ("Lebanon Hills", ["Lebanon Hills"]),
        ("lebanon-hills", ["Lebanon Hills"]),
        ("lebanon", ["Lebanon Hills"]),
        ("wirth", ["Theodore Wirth: MOCA/MORC Trails"]),
        ("Theo Wirth", ["Theodore Wirth: MOCA/MORC Trails"]),
        ("murphy", ["Murphy Hanrehan"]),
        ("murphy hanrahan", ["Murphy Hanrehan"]),  # common misspelling
        ("elm creek", ["Elm Creek"]),
        ("battle creek", ["Battle Creek"]),
        ("salem", ["Salem Hills"]),
        ("lebannon hils", ["Lebanon Hills"]),
    ],
)
def test_match(query, expected):
    assert names(query) == expected


def test_ambiguous_returns_all():
    assert set(names("creek")) == {"Battle Creek", "Elm Creek", "Rice Creek Chain of Lakes"}


def test_no_match():
    assert names("cuyuna") == []


def test_summary_mentions_status_tags_and_note():
    bc = next(t for t in TRAILS if t.slug == "battle-creek")
    s = bc.summary()
    assert s.startswith("Battle Creek is OPEN (conditions: caution).")
    assert "Maintainer note:" in s and "updated" in s


def _mock_client(handler):
    http = httpx.AsyncClient(base_url="https://trailbot.com", transport=httpx.MockTransport(handler))
    return TrailbotClient(ttl_seconds=300, http=http)


async def test_client_caches_and_serves_stale_on_failure():
    calls = {"n": 0, "fail": False}

    def handler(req):
        calls["n"] += 1
        if calls["fail"]:
            return httpx.Response(503)
        return httpx.Response(200, text=HTML)

    c = _mock_client(handler)
    t1, stale1 = await c.trails("morc")
    t2, _ = await c.trails("MORC")
    assert calls["n"] == 1 and not stale1 and t1 is t2

    c.ttl = 0
    calls["fail"] = True
    t3, stale3 = await c.trails("morc")
    assert stale3 and t3 is t1


async def test_unknown_org():
    c = _mock_client(lambda req: httpx.Response(404))
    with pytest.raises(TrailbotError, match="no organization"):
        await c.trails("nope")


async def test_get_trail_status_tool(monkeypatch):
    monkeypatch.setattr(server, "client", _mock_client(lambda req: httpx.Response(200, text=HTML)))
    r = await server.get_trail_status("lake rebecca")
    assert r["matches"][0]["open_for_riding"] is False
    assert r["summary"].startswith("Lake Rebecca is CLOSED (conditions: greasy, wet).")

    r = await server.get_trail_status("cuyuna")
    assert r["matches"] == [] and "Known trails" in r["summary"]


async def test_list_trails_tool(monkeypatch):
    monkeypatch.setattr(server, "client", _mock_client(lambda req: httpx.Response(200, text=HTML)))
    r = await server.list_trails(status="closed")
    assert r["count"] == r["closed"] == 6
    assert all(t["open_for_riding"] is False for t in r["trails"])
