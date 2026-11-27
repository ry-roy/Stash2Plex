"""Build Thumbor URLs for Plex actor thumbnails.

Plex has no actor-image upload endpoint: the only way to set an actor thumb is
the low-level ``actor[N].tag.thumb`` key, which takes a URL that the Plex server
fetches for itself. Posters sidestep that by downloading the bytes and uploading
them (see Worker._upload_image), but actors cannot. So we hand Plex a Thumbor URL
and let Thumbor do the face-aware square crop on demand.
"""

from urllib.parse import quote

from poster_source import _rebase

# Thumbor's smart crop runs its configured detectors (face first) and centres the
# crop on what it finds, falling back to entropy when it finds nothing. This is the
# fallback when no transform is configured, so it stays on stock Thumbor features.
DEFAULT_TRANSFORM = "300x300/smart"


def performer_thumb_url(image_path, thumbor_base, source_base=None, transform=None):
    """Wrap a Stash performer image URL in an on-demand Thumbor face crop.

    Args:
        image_path: Stash performer ``image_path``, or None.
        thumbor_base: Origin of the Thumbor server, e.g. ``http://thumbor.lan:8001``.
            Falsy disables the feature entirely — this is the on/off switch.
        source_base: Optional origin rewrite for ``image_path``. Stash hands out
            URLs on the address *this plugin* reaches it on (often 127.0.0.1),
            which means something different inside the Thumbor container. Set
            this to an address Thumbor can resolve, e.g. ``http://stash:9999``.
        transform: Optional Thumbor transform path, e.g.
            ``400x400/smart/filters:face_zoom(0.5)``. Falsy uses
            ``DEFAULT_TRANSFORM``. Callers must hand this over already
            validated (see Stash2PlexConfig.normalize_transform) — it goes
            into the path verbatim.

    Returns:
        The Thumbor URL, or None when there is no image or no Thumbor configured.
    """
    image_path = (image_path or '').strip()
    thumbor_base = (thumbor_base or '').strip()
    if not image_path or not thumbor_base:
        return None

    transform = (transform or '').strip().strip('/') or DEFAULT_TRANSFORM
    source = _rebase(image_path, source_base) if source_base else image_path
    # Encode the whole source URL into a single path segment; its own scheme,
    # slashes and query string must not be read as part of the Thumbor path.
    return f"{thumbor_base.rstrip('/')}/unsafe/{transform}/{quote(source, safe='')}"
