from pathlib import Path

import httpx
import pytest

from trailbot_mcp import server
from trailbot_mcp.lookup import match_trails, resolve_location, split_trail_and_place
from trailbot_mcp.trailbot import TrailbotClient, TrailbotError, parse_index, parse_org_trails

FIXTURES = Path(__file__).parent / "fixtures"
MORC_JSON = (FIXTURES / "morc_trails.json").read_text()
INDEX_HTML = (FIXTURES / "index.html").read_text()
MORC = parse_org_trails(MORC_JSON, "morc")
INDEX = parse_index(INDEX_HTML)


def names(q, trails=MORC):
    return [t.name for t in match_trails(q, trails)]


# --- parsing ---------------------------------------------------------------


def test_parse_org_trails():
    assert len(MORC) == 19
    rebecca = next(t for t in MORC if t.slug == "lake-rebecca")
    assert rebecca.open_for_riding is False
    assert rebecca.tags == ["greasy", "wet"]


def test_parse_index():
    assert len(INDEX.trails) == 330
    assert INDEX.areas["MN"].name == "Minnesota"
    hillside = next(t for t in INDEX.trails if t.slug == "hillside-park")
    assert (hillside.org, hillside.state) == ("morc", "MN")
    # "Ontario" normalized to "ON"
    assert {t.state for t in INDEX.trails} >= {"ON"} and "ONTARIO" not in {t.state for t in INDEX.trails}


def test_parse_rejects_unexpected_responses():
    with pytest.raises(TrailbotError):
        parse_index("<html>nope</html>")
    with pytest.raises(TrailbotError):
        parse_org_trails("<html>nope</html>", "morc")
    with pytest.raises(TrailbotError):
        parse_org_trails('{"unexpected": []}', "morc")


# --- trail name matching ---------------------------------------------------


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
        ("salem", ["Salem Hills"]),
        ("lebannon hils", ["Lebanon Hills"]),
    ],
)
def test_match(query, expected):
    assert names(query) == expected


def test_ambiguous_returns_all():
    assert set(names("creek")) == {"Battle Creek", "Elm Creek", "Rice Creek Chain of Lakes"}


def test_generic_words_alone_dont_match():
    assert names("hillside trails") == ["Hillside Park"]
    wi = [t for t in INDEX.trails if t.state == "WI"]
    assert names("lebanon hills", wi) == []  # not "Veteran Hills" / "Timberland Hills"


@pytest.mark.parametrize(
    "query,expected",
    [
        ("theo worth", ["Theodore Wirth: Loppet Trails", "Theodore Wirth: MOCA/MORC Trails"]),
        ("murphy hanrahan", ["Murphy Hanrehan"]),
        ("hillside", ["Hillside Park"]),
        ("lester", ["Lester Park Trail Center/Downer"]),
        ("spirit mtn", ["Spirit Mountain Bike Park"]),
    ],
)
def test_match_across_whole_index(query, expected):
    assert names(query, INDEX.trails) == expected


def test_no_status_set_points_to_note():
    t = parse_org_trails(MORC_JSON, "morc")[0]
    t.status, t.note = "Unknown", "All trails are open."
    assert "not marked open or closed" in t.summary() and "All trails are open." in t.summary()


def test_no_match():
    assert names("zzzz") == []


# --- places ----------------------------------------------------------------


@pytest.mark.parametrize(
    "place,label,count",
    [
        ("minnesota", "Minnesota", 82),
        ("MN", "Minnesota", 82),
        ("in minnesota", "Minnesota", 82),
        ("twin cities", "Twin Cities Metro", 30),
        ("duluth", "Duluth/Northeast MN", 24),
        ("near duluth", "Duluth/Northeast MN", 24),
        ("southeast minnesota", "Southeast MN", 12),
        ("northwest minnesota", "Northwest MN", 10),
        ("elk river", "Elk River, MN", 1),
        ("elk river, mn", "Elk River, MN", 1),
        ("saint paul", "St Paul, MN", 2),
        ("ontario", "Ontario", 8),
    ],
)
def test_resolve_location(place, label, count):
    got = resolve_location(place, INDEX)
    assert got and got[0] == label and len(got[1]) == count


def test_unknown_place():
    assert resolve_location("atlantis", INDEX) is None


def test_split_trail_and_place():
    name, label, pool = split_trail_and_place("hillside in minnesota", INDEX)
    assert (name, label) == ("hillside", "Minnesota")
    assert names(name, pool) == ["Hillside Park"]
    assert split_trail_and_place("hillside", INDEX) is None


# --- client ----------------------------------------------------------------


def _routes(req: httpx.Request) -> httpx.Response:
    if req.url.path == "/trails":
        return httpx.Response(200, text=INDEX_HTML)
    if req.url.path == "/api/public/organizations/morc/trails":
        return httpx.Response(200, text=MORC_JSON)
    return httpx.Response(404)


def _mock_client(handler=_routes):
    http = httpx.AsyncClient(base_url="https://trailbot.com", transport=httpx.MockTransport(handler))
    return TrailbotClient(ttl_seconds=300, http=http)


async def test_client_caches_and_serves_stale_on_failure():
    calls = {"n": 0, "fail": False}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(503) if calls["fail"] else httpx.Response(200, text=MORC_JSON)

    c = _mock_client(handler)
    t1, stale1 = await c.trails("morc")
    t2, _ = await c.trails("morc")
    assert calls["n"] == 1 and not stale1 and t1 is t2

    c.org_cache.ttl = 0
    calls["fail"] = True
    t3, stale3 = await c.trails("morc")
    assert stale3 and t3 is t1


async def test_unknown_org():
    with pytest.raises(TrailbotError, match="not found"):
        await _mock_client().trails("nope")


# --- tools -----------------------------------------------------------------


@pytest.fixture
def mocked(monkeypatch):
    monkeypatch.setattr(server, "client", _mock_client())
    monkeypatch.setattr(server, "HOME", None)


async def test_status_with_place_in_trail_text(mocked):
    r = await server.get_trail_status("hillside in minnesota")
    assert len(r["matches"]) == 1
    assert r["summary"].startswith("Hillside Park (")
    assert r["matches"][0]["open_for_riding"] in (True, False)


async def test_status_with_location_arg(mocked):
    r = await server.get_trail_status("lake rebecca", location="twin cities")
    assert r["summary"].startswith("Lake Rebecca (Rockford, MN) is CLOSED, conditions: greasy, wet.")


async def test_status_without_location_searches_everywhere(mocked):
    r = await server.get_trail_status("lebanon hills")
    assert [m["name"] for m in r["matches"]] == ["Lebanon Hills"]


async def test_status_wrong_place_falls_back(mocked):
    r = await server.get_trail_status("lebanon hills", location="wisconsin")
    assert r["summary"].startswith("No trail matching 'lebanon hills' in Wisconsin")
    assert r["matches"][0]["name"] == "Lebanon Hills"


async def test_status_unknown_trail_and_place(mocked):
    assert (await server.get_trail_status("zzzz"))["matches"] == []
    assert "Couldn't find a place" in (await server.get_trail_status("x", location="atlantis"))["summary"]


async def test_status_org_unavailable(mocked):
    # Spirit Mountain isn't MORC; the mock 404s its org page.
    r = await server.get_trail_status("spirit mountain")
    assert r["matches"][0]["status"] == "Unknown"
    assert r["warnings"]


async def test_home_preference(mocked, monkeypatch):
    monkeypatch.setattr(server, "HOME", "Twin Cities Metro")
    r = await server.get_trail_status("battle creek")
    assert [m["name"] for m in r["matches"]] == ["Battle Creek"]


async def test_list_trails_region(mocked):
    r = await server.list_trails("elk river")
    assert r["summary"].startswith("Elk River, MN:")
    assert [t["name"] for t in r["trails"]] == ["Hillside Park"]


async def test_list_trails_closed_filter(mocked):
    r = await server.list_trails("twin cities", status="closed")
    assert r["trails"] and all(t["open_for_riding"] is False for t in r["trails"])
    assert "CLOSED" in r["summary"] and "OPEN" not in r["summary"]
    assert any("Status unavailable" in w for w in r["warnings"])  # non-MORC orgs 404 in the mock


async def test_list_trails_too_broad(mocked):
    r = await server.list_trails("minnesota")
    assert "too many" in r["summary"] and "Twin Cities Metro" in r["regions"]


async def test_list_trails_needs_place(mocked):
    assert "Which area" in (await server.list_trails())["summary"]


async def test_healthz_reports_commit(monkeypatch):
    from starlette.testclient import TestClient

    monkeypatch.setenv("GIT_COMMIT", "abc123")
    monkeypatch.setenv("MCP_AUTH_TOKEN", "secret")
    with TestClient(server.build_app()) as c:
        assert c.get("/healthz").json() == {"status": "ok", "commit": "abc123"}
        assert c.post("/mcp").status_code == 401  # healthz is public, MCP is not
