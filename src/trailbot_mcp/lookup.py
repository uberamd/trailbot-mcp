"""Natural-language matching of trail names and places against the Trailbot index."""

from __future__ import annotations

import difflib
import re
from typing import Protocol, TypeVar

from .trailbot import Index, IndexTrail

_FILLER = re.compile(r"^(?:in|near|around|at|by|the)\s+")
_SPLIT = re.compile(r"\s+(?:in|near|around|by)\s+", re.I)


class Named(Protocol):
    name: str
    slug: str


N = TypeVar("N", bound=Named)


def norm(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()
    return re.sub(r"\bsaint\b", "st", s)


# Words too common in trail names to identify one on their own.
_GENERIC = set(
    "trail trails park parks hill hills lake lakes creek river mountain mtn state county regional "
    "bike mtb singletrack area system center rec recreation forest the and of".split()
)

EXACT, ALL_WORDS, CLOSE, WEAK = 3, 2, 1, 0


def match_trails(query: str, trails: list[N]) -> list[N]:
    return match_trails_scored(query, trails)[1]


def match_trails_scored(query: str, trails: list[N]) -> tuple[int, list[N]]:
    """Best-effort fuzzy lookup. Returns (strength, matches); strength is EXACT..WEAK."""
    q = norm(query)
    if not q:
        return WEAK, []
    exact = [t for t in trails if q in (norm(t.name), norm(t.slug))]
    if exact:
        return EXACT, exact
    words = q.split()
    hits = [t for t in trails if all(w in norm(t.name) or w in norm(t.slug) for w in words)]
    if hits:
        return ALL_WORDS, hits
    names: dict[str, list[N]] = {}
    for t in trails:
        names.setdefault(norm(t.name), []).append(t)
    close = difflib.get_close_matches(q, names, n=3, cutoff=0.75)
    if close:
        return CLOSE, [t for c in close for t in names[c]]
    # Last resort: count distinctive query words that closely match a word in the name
    # (handles "murphy hanrahan", "hillside trails", "theo worth").
    distinctive = [w for w in words if len(w) > 3 and w not in _GENERIC]
    scored = []
    for t in trails:
        name_words = [w for w in norm(t.name).split() if w not in _GENERIC]
        score = sum(
            1
            for w in distinctive
            if any(n.startswith(w) for n in name_words)
            or difflib.get_close_matches(w, name_words, n=1, cutoff=0.8)
        )
        if score:
            scored.append((score, t))
    if not scored:
        return WEAK, []
    best = max(s for s, _ in scored)
    return WEAK, [t for s, t in scored if s == best]


def resolve_location(text: str, index: Index) -> tuple[str, list[IndexTrail]] | None:
    """Turn 'minnesota', 'MN', 'twin cities', 'duluth', 'elk river, mn' into (label, trails)."""
    return _resolve(norm(text), index, index.trails)


def _resolve(q: str, index: Index, pool: list[IndexTrail]) -> tuple[str, list[IndexTrail]] | None:
    while (stripped := _FILLER.sub("", q)) != q:
        q = stripped
    if not q:
        return None

    # State / province: "mn" or "minnesota"
    for code, area in index.areas.items():
        if q in (code.lower(), norm(area.name)):
            trails = [t for t in pool if t.state == code]
            return (area.name, trails) if trails else None

    # Region (key or display name, allowing a subset of words: "twin cities" -> "Twin Cities Metro")
    # or city. Duluth is both, so union them.
    words = set(_canon(q, index).split())
    labels, region_keys = [], set()
    for area in index.areas.values():
        for key, display in area.regions.items():
            if key.endswith("-All"):
                continue
            cand = set(_canon(norm(key), index).split()) | set(_canon(norm(display), index).split())
            if words <= cand and key not in region_keys:
                region_keys.add(key)
                labels.append(display)
    hits = [t for t in pool if region_keys.intersection(t.regions) or norm(t.city) == q]
    if hits:
        if not labels:
            labels = [f"{hits[0].city}, {hits[0].state}"]
        return " / ".join(labels), hits

    # Trailing state qualifier: "elk river mn", "duluth, minnesota", "southeast minnesota"
    for code, area in index.areas.items():
        for suffix in (norm(area.name), code.lower()):
            if q.endswith(" " + suffix):
                in_state = [t for t in pool if t.state == code]
                found = _resolve(q[: -len(suffix)].strip(), index, in_state)
                if found:
                    label = found[0] if found[0].endswith(f", {code}") else f"{found[0]}, {area.name}"
                    return label, found[1]
    return None


def _canon(s: str, index: Index) -> str:
    """Replace state names with codes so 'northwest minnesota' and 'Northwest MN' compare equal."""
    for code, area in sorted(index.areas.items(), key=lambda kv: -len(kv[1].name)):
        s = re.sub(rf"\b{re.escape(norm(area.name))}\b", code.lower(), s)
    return s


def split_trail_and_place(query: str, index: Index) -> tuple[str, str, list[IndexTrail]] | None:
    """'hillside in minnesota' -> ('hillside', 'Minnesota', [...]) if the tail is a known place."""
    parts = _SPLIT.split(query)
    for i in range(len(parts) - 1, 0, -1):
        name = " ".join(parts[:i])
        place = " ".join(parts[i:])
        found = resolve_location(place, index)
        if found:
            return name, found[0], found[1]
    return None
