# Performer thumbnails

Stash performer images become Plex actor thumbnails, square-cropped on their
faces by a [Thumbor](https://thumbor.readthedocs.io/) server you run yourself.

Disabled by default: leave **Performer Thumbor Base URL** empty and nothing
changes.

## Why a URL instead of an upload

Plex has no actor-image upload endpoint. The only way to set an actor thumbnail
is the low-level `actor[N].tag.thumb` metadata key, which takes a URL that **the
Plex server fetches for itself**.

Posters and backgrounds avoid that problem by downloading the bytes from Stash
and uploading them to Plex (`Worker._upload_image`), which is why they work even
when Plex cannot reach Stash. Actors have no equivalent, so the URL we hand Plex
has to be reachable from Plex, and whatever is behind it has to do the cropping.
Thumbor does both, on demand, with nothing stored on our side.

The generated URL looks like:

```text
<thumbor-base>/unsafe/300x300/smart/<percent-encoded-stash-image-url>
```

`smart` runs Thumbor's configured detectors — put `thumbor.detectors.face_detector`
first — and centres the crop on the face it finds, falling back to entropy when
it finds none.

## Settings

| Setting | Example | Purpose |
|---|---|---|
| Performer Thumbor Base URL | `http://thumbor.lan:8001` | Your Thumbor origin. Empty disables the feature. |
| Performer Image Source Base URL | `http://stash:9999` | Origin Thumbor should use to reach Stash. Optional. |

### Why the second setting exists

Stash reports image URLs on the address *this plugin* reaches it on — commonly
`http://127.0.0.1:9999`. Inside the Thumbor container, `127.0.0.1` is Thumbor
itself, so the fetch fails or returns the wrong thing.

Setting a source base rewrites **only** the scheme, host and port; the performer
path and its `?t=` cache-busting query are preserved. Use whatever address
Thumbor can actually resolve — a Docker service name if they share a network, or
a LAN hostname otherwise. Leave it empty if Stash's own address already works.

## Requirements

- Thumbor must be able to fetch the decoded Stash URL. Plex only ever sees the
  outer Thumbor URL.
- `ALLOW_UNSAFE_URL = True` in `thumbor.conf`. These are unsigned URLs; a
  deployment that requires signed URLs needs the helper reworked around its
  security key.
- If Stash requires authentication for images, Thumbor will get 401/403 —
  credentials are not forwarded.

## Behaviour notes

- Performers without an image still sync by name; they simply get no thumbnail.
- Thumbnails are sent in the same Plex edit as the actor names, so this costs no
  additional Plex request.
- The edit also fires when only thumbnails changed. Performers whose names had
  already synced would otherwise never receive artwork.
- Thumbnails are re-sent on each sync rather than diffed. Plex rewrites
  `actor.thumb` to its own cached URL once it fetches the image, so there is
  nothing stable to compare against. Re-sending is idempotent and repairs the
  thumbnail if a Plex agent refresh overwrites it.
- Stash's `?t=` timestamp changes when a performer image changes, which yields a
  new Thumbor URL and defeats Plex's caching for free.

## Troubleshooting

Check in this order:

1. **Did the scene reach the worker?** Look for
   `Updated performer thumbnail: <name> -> <url>` in the logs. No line means the
   job never got that far — debug the queue, not Thumbor.
2. **Can Thumbor fetch the source?** Percent-decode the last path segment of the
   generated URL and request it from Thumbor's own network context.
3. **Does the URL render?** Open the Thumbor URL directly in a browser. A 200 and
   a 300×300 image means the plugin's work is done.
4. **Only then look at Plex**, which caches artwork aggressively.
