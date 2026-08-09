"""Poster resolution for Stash2Plex.

Decides *which* image should become a scene's Plex poster, in priority order:

    1. custom_field - a hand-cropped 2:3 poster saved by PosterManager
    2. group        - the front image of the Group (formerly Movie) the scene
                      belongs to. Already portrait, already scraped: free
                      poster art for anything that's part of a release.
    3. cover        - the 16:9 scene cover. Stash2Plex's current behaviour,
                      kept as the last resort.

Self-contained: standard library only, no imports from Stash2Plex. Drop it in
the plugin folder and call resolve_poster() from the poster sync path. See
INTEGRATION.md for the wiring.
"""

from __future__ import annotations

import hashlib
import json
import urllib.parse
import urllib.request
from typing import Callable, Optional, Sequence, Tuple

DEFAULT_ORDER = ("custom_field", "group", "cover")
DEFAULT_CUSTOM_FIELD = "poster"
MAX_BYTES = 25 * 1024 * 1024
TIMEOUT = 30

# Drop these into any scene query whose result gets handed to
# poster_url_from_scene(). Both are additive: an unpatched query simply yields
# neither key and the cascade falls through to the cover, i.e. today's
# behaviour. `groups` needs Stash >= 0.27; older servers should use `movies {
# movie { id name front_image_path } }`, which the resolver also understands.
SCENE_FIELDS = """
    custom_fields
    groups { group { id name front_image_path } }
"""

# Stash serves a placeholder when an entity has no real image, flagged in the
# query string. Pushing those to Plex would replace good art with a grey box.
_PLACEHOLDER_MARKERS = ("default=true", "default=1")

_SCENE_QUERY = """
query S2PPosterSource($id: ID!) {
  findScene(id: $id) {
    id
    custom_fields
    paths { screenshot }
    groups { group { id name front_image_path } }
  }
}
"""

# Stash < 0.27 called them movies, and exposed no custom fields on scenes.
_SCENE_QUERY_LEGACY = """
query S2PPosterSource($id: ID!) {
  findScene(id: $id) {
    id
    paths { screenshot }
    movies { movie { id name front_image_path } }
  }
}
"""


class PosterCandidate:
    """A resolved poster: where it came from and how to get the bytes."""

    def __init__(
        self,
        origin: str,
        url: str,
        label: str = "",
        api_key: str = "",
        session_cookie: Optional[dict] = None,
    ):
        self.origin = origin  # "custom_field" | "group" | "cover"
        self.url = url
        self.label = label  # e.g. the group name, for logging
        self._api_key = api_key
        self._session_cookie = session_cookie
        self._bytes: Optional[bytes] = None

    def fetch(self) -> bytes:
        """Download the image. Cached, so calling twice is cheap."""
        if self._bytes is None:
            self._bytes = _download(self.url, self._api_key, self._session_cookie)
        return self._bytes

    @property
    def sha256(self) -> str:
        """Content hash - store it to skip re-uploading an unchanged poster."""
        return hashlib.sha256(self.fetch()).hexdigest()

    def __repr__(self) -> str:
        suffix = " ({})".format(self.label) if self.label else ""
        return "<PosterCandidate {}{}>".format(self.origin, suffix)


def resolve_poster(
    gql: Callable[[str, dict], dict],
    scene_id,
    *,
    order: Sequence[str] = DEFAULT_ORDER,
    custom_field: str = DEFAULT_CUSTOM_FIELD,
    stash_url: str = "",
    api_key: str = "",
    session_cookie: Optional[dict] = None,
    log=None,
) -> Optional[PosterCandidate]:
    """Return the best poster for a scene, or None if nothing usable exists.

    gql       - callable taking (query, variables) and returning the `data`
                dict. Stash2Plex's existing Stash client can be adapted with a
                one-line lambda; see INTEGRATION.md.
    order     - which sources to try, in order. Drop entries to disable them,
                e.g. ("group", "cover") to ignore hand-cropped posters.
    stash_url - base URL Stash2Plex uses to reach Stash. Image paths come back
                pointing at whatever host Stash thinks it is, which is wrong
                across containers, so they get rebased onto this.
    api_key   - Stash API key, appended to image URLs when Stash has auth on.
    session_cookie
              - the SessionCookie dict Stash hands plugins in server_connection.
                Use this instead of api_key when running inside a Stash plugin;
                see from_server_connection().
    """
    scene = _load_scene(gql, scene_id, log=log)
    if not scene:
        return None

    builders = {
        "custom_field": lambda: _from_custom_field(scene, custom_field),
        "group": lambda: _from_group(scene),
        "cover": lambda: _from_cover(scene),
    }

    for source in order:
        build = builders.get(source)
        if build is None:
            _log(log, "warn", "unknown poster source {!r}, skipping".format(source))
            continue

        found = build()
        if not found:
            continue

        raw_url, label = found
        if _is_placeholder(raw_url):
            _log(log, "debug", "{} image is a Stash placeholder, skipping".format(source))
            continue

        url = _rebase(raw_url, stash_url)
        _log(log, "debug", "poster source for scene {}: {} {}".format(scene_id, source, label))
        return PosterCandidate(
            source, url, label=label, api_key=api_key, session_cookie=session_cookie
        )

    return None


def poster_url_from_scene(
    scene: dict,
    *,
    order: Sequence[str] = DEFAULT_ORDER,
    custom_field: str = DEFAULT_CUSTOM_FIELD,
    log=None,
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Pick a poster URL out of an already-fetched scene. No I/O.

    The non-fetching sibling of resolve_poster(), for callers that already have
    the scene dict in hand and hand the URL to their own downloader.

    Returns (url, origin, label); (None, None, None) if nothing usable exists.

    Relative URLs - which is what PosterManager writes into the custom field -
    are resolved against the scene's own paths.screenshot, so the result comes
    back absolute and on whatever host this caller already reaches Stash on.
    Add SCENE_FIELDS to the scene query to make the first two sources visible;
    without them this degrades to returning the cover.
    """
    builders = {
        "custom_field": lambda: _from_custom_field(scene, custom_field),
        "group": lambda: _from_group(scene),
        "cover": lambda: _from_cover(scene),
    }

    for source in order:
        build = builders.get(source)
        if build is None:
            _log(log, "warn", "unknown poster source {!r}, skipping".format(source))
            continue

        found = build()
        if not found:
            continue

        raw_url, label = found
        if _is_placeholder(raw_url):
            _log(log, "debug", "{} image is a Stash placeholder, skipping".format(source))
            continue

        url = _absolutize(raw_url, scene)
        if not url:
            continue

        _log(log, "debug", "poster source for scene {}: {} {}".format(
            scene.get("id"), source, label))
        return url, source, label

    return None, None, None


# --------------------------------------------------------------------------
# Source builders. Each returns (url, label) or None.
# --------------------------------------------------------------------------


def _from_custom_field(scene: dict, field_name: str):
    fields = scene.get("custom_fields") or {}
    value = fields.get(field_name)
    if isinstance(value, str) and value.strip():
        return value.strip(), "hand-cropped"
    return None


def _from_group(scene: dict):
    """First group with a real front image wins.

    Groups are ordered as Stash returns them; a scene in several groups is rare
    and there's no meaningful way to rank them, so first-wins is the honest
    behaviour rather than a guess dressed up as logic.
    """
    entries = scene.get("groups")
    key = "group"
    if entries is None:  # legacy schema
        entries = scene.get("movies") or []
        key = "movie"

    for entry in entries:
        group = (entry or {}).get(key) or {}
        path = group.get("front_image_path")
        if path and not _is_placeholder(path):
            return path, group.get("name") or "group {}".format(group.get("id"))
    return None


def _from_cover(scene: dict):
    path = ((scene.get("paths") or {}).get("screenshot")) or ""
    if path:
        return path, "scene cover"
    return None


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _load_scene(gql, scene_id, log=None) -> Optional[dict]:
    try:
        data = gql(_SCENE_QUERY, {"id": str(scene_id)})
        return (data or {}).get("findScene")
    except Exception as exc:
        _log(log, "debug", "modern scene query failed ({}), trying legacy".format(exc))

    try:
        data = gql(_SCENE_QUERY_LEGACY, {"id": str(scene_id)})
        return (data or {}).get("findScene")
    except Exception as exc:
        _log(log, "error", "could not load scene {}: {}".format(scene_id, exc))
        return None


def _is_placeholder(url: str) -> bool:
    lowered = (url or "").lower()
    return any(marker in lowered for marker in _PLACEHOLDER_MARKERS)


def _rebase(url: str, stash_url: str) -> str:
    """Point a Stash-supplied URL at the host we actually reach Stash on."""
    if not stash_url:
        return url
    parsed = urllib.parse.urlparse(url)
    if not parsed.scheme:  # already relative
        return stash_url.rstrip("/") + "/" + url.lstrip("/")
    base = urllib.parse.urlparse(stash_url)
    return urllib.parse.urlunparse(
        (base.scheme or parsed.scheme, base.netloc or parsed.netloc,
         parsed.path, parsed.params, parsed.query, parsed.fragment)
    )


def _absolutize(url: str, scene: dict) -> Optional[str]:
    """Make a scene-supplied URL absolute using the scene's own cover as base.

    paths.screenshot is always absolute and always points at the host this
    process reaches Stash on, which makes it a free, always-correct base URL -
    no new setting, nothing to keep in sync with the container topology.
    """
    url = (url or "").strip()
    if not url:
        return None
    if urllib.parse.urlparse(url).scheme:
        return url

    cover = ((scene.get("paths") or {}).get("screenshot")) or ""
    base = urllib.parse.urlparse(cover)
    if not (base.scheme and base.netloc):
        return None
    return urllib.parse.urljoin(
        "{}://{}".format(base.scheme, base.netloc), url
    )


def _with_api_key(url: str, api_key: str) -> str:
    if not api_key:
        return url
    parsed = urllib.parse.urlparse(url)
    query = dict(urllib.parse.parse_qsl(parsed.query))
    query.setdefault("apikey", api_key)
    return urllib.parse.urlunparse(
        parsed._replace(query=urllib.parse.urlencode(query))
    )


def _download(url: str, api_key: str = "", session_cookie: Optional[dict] = None) -> bytes:
    headers = {"User-Agent": "Stash2Plex/poster_source"}
    if session_cookie and session_cookie.get("Name"):
        headers["Cookie"] = "{}={}".format(
            session_cookie["Name"], session_cookie.get("Value", "")
        )
    request = urllib.request.Request(_with_api_key(url, api_key), headers=headers)
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        blob = response.read(MAX_BYTES + 1)
    if len(blob) > MAX_BYTES:
        raise ValueError("Poster at {} exceeds 25 MB".format(url))
    if not blob:
        raise ValueError("Poster at {} was empty".format(url))
    return blob


def _log(log, level: str, message: str) -> None:
    if log is None:
        return
    getattr(log, level, getattr(log, "info", lambda _m: None))(
        "[poster_source] " + message
    )


def from_server_connection(server_connection: dict):
    """Pull the Stash base URL and session cookie out of a plugin payload.

    Stash passes a "server_connection" block to every plugin it invokes. Using
    it means the patch needs no new configuration and keeps working if the port
    or scheme changes.

        payload = json.loads(sys.stdin.read())
        stash_url, cookie = from_server_connection(payload["server_connection"])
    """
    conn = server_connection or {}
    scheme = (conn.get("Scheme") or "http").lower()
    host = conn.get("Host") or "localhost"
    if host in ("0.0.0.0", "::"):
        host = "localhost"
    port = conn.get("Port")
    base = "{}://{}".format(scheme, host)
    if port:
        base += ":{}".format(port)
    return base, conn.get("SessionCookie")
