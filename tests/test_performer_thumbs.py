"""
Unit tests for Thumbor-backed performer thumbnails.

Tests verify:
- The Thumbor base URL acts as the feature switch
- The whole source URL is percent-encoded into one path segment
- Origin rewriting preserves the path and query string
- Names survive sanitisation on their way to the thumb lookup
- Performers already present in Plex still receive thumbnails
- Performers without an image still sync by name
"""

import pytest

from shared.thumbor import performer_thumb_url


IMAGE = "http://127.0.0.1:9999/performer/20/image?t=1784953486&default=true"
THUMBOR = "http://thumbor.test:8001"


class TestPerformerThumbUrl:
    """Tests for the URL builder."""

    @pytest.mark.parametrize("image_path", [None, "", "   "])
    def test_missing_image_returns_none(self, image_path):
        """No image means no thumb, never a malformed URL."""
        assert performer_thumb_url(image_path, THUMBOR) is None

    @pytest.mark.parametrize("thumbor_base", [None, ""])
    def test_unset_thumbor_base_disables_feature(self, thumbor_base):
        """An unconfigured Thumbor base is the off switch."""
        assert performer_thumb_url(IMAGE, thumbor_base) is None

    def test_source_url_is_encoded_into_a_single_segment(self):
        """The source URL's own scheme/slashes/query must not split the path."""
        url = performer_thumb_url(IMAGE, THUMBOR)

        assert url == (
            "http://thumbor.test:8001/unsafe/300x300/smart/"
            "http%3A%2F%2F127.0.0.1%3A9999%2Fperformer%2F20%2Fimage"
            "%3Ft%3D1784953486%26default%3Dtrue"
        )
        # Everything after the smart/ marker is exactly one segment.
        assert url.split("/smart/", 1)[1].count("/") == 0

    def test_trailing_slash_on_base_does_not_double_up(self):
        assert "//unsafe" not in performer_thumb_url(IMAGE, THUMBOR + "/")

    def test_source_base_rewrites_origin_only(self):
        """Only scheme/host/port change; path and query survive intact."""
        url = performer_thumb_url(IMAGE, THUMBOR, source_base="http://stash:9999")

        encoded = url.split("/smart/", 1)[1]
        assert encoded.startswith("http%3A%2F%2Fstash%3A9999")
        assert "%2Fperformer%2F20%2Fimage" in encoded
        assert "%3Ft%3D1784953486%26default%3Dtrue" in encoded
        assert "127.0.0.1" not in url

    def test_without_source_base_the_stash_address_is_kept(self):
        assert "127.0.0.1" in performer_thumb_url(IMAGE, THUMBOR)


class TestSceneExtractor:
    """Performer images must reach the queue keyed by name."""

    def test_extracts_name_to_image_map(self):
        from validation.scene_extractor import extract_scene_metadata

        data = extract_scene_metadata({
            'title': 'Scene',
            'performers': [
                {'name': 'Alice', 'image_path': IMAGE},
                {'name': 'Bob'},
            ],
        })

        assert data['performers'] == ['Alice', 'Bob']
        # Bob has no image and must not appear, but must still sync by name.
        assert data['performer_images'] == {'Alice': IMAGE}

    def test_no_images_omits_the_key_entirely(self):
        from validation.scene_extractor import extract_scene_metadata

        data = extract_scene_metadata({
            'title': 'Scene',
            'performers': [{'name': 'Alice'}],
        })

        assert 'performer_images' not in data


class _FakeActor:
    def __init__(self, tag):
        self.tag = tag


class _FakePlexItem:
    def __init__(self, actors=()):
        self.actors = [_FakeActor(a) for a in actors]
        self.edits = None

    def edit(self, **kwargs):
        self.edits = kwargs


class _FakeResult:
    def __init__(self):
        self.successes = []
        self.warnings = []

    def add_success(self, field):
        self.successes.append(field)

    def add_warning(self, field, err):
        self.warnings.append((field, err))


def _worker(thumbor_base=THUMBOR, source_base=None):
    """A SyncWorker with only the config _sync_performers touches."""
    from worker.processor import SyncWorker

    worker = SyncWorker.__new__(SyncWorker)
    worker.config = type('Config', (), {
        'thumbor_base_url': thumbor_base,
        'performer_image_source_base_url': source_base,
    })()
    return worker


class TestSyncPerformers:
    """Tests for the Plex actor edit."""

    def test_thumb_accompanies_the_name_edit(self):
        item = _FakePlexItem()
        needs_reload = _worker()._sync_performers(
            item,
            {'performers': ['Alice'], 'performer_images': {'Alice': IMAGE}},
            _FakeResult(), False,
        )

        assert needs_reload is True
        assert item.edits['actor[0].tag.tag'] == 'Alice'
        assert item.edits['actor[0].tag.thumb'].startswith(THUMBOR)

    def test_existing_actor_still_gets_a_thumb(self):
        """The regression this feature exists for: names already synced."""
        item = _FakePlexItem(actors=['Alice'])
        needs_reload = _worker()._sync_performers(
            item,
            {'performers': ['Alice'], 'performer_images': {'Alice': IMAGE}},
            _FakeResult(), False,
        )

        assert needs_reload is True
        assert item.edits['actor[0].tag.thumb'].startswith(THUMBOR)

    def test_no_edit_when_names_match_and_no_images(self):
        """Without images this must stay a no-op, exactly as before."""
        item = _FakePlexItem(actors=['Alice'])
        needs_reload = _worker()._sync_performers(
            item, {'performers': ['Alice']}, _FakeResult(), False,
        )

        assert needs_reload is False
        assert item.edits is None

    def test_unconfigured_thumbor_sends_names_only(self):
        item = _FakePlexItem()
        _worker(thumbor_base=None)._sync_performers(
            item,
            {'performers': ['Alice'], 'performer_images': {'Alice': IMAGE}},
            _FakeResult(), False,
        )

        assert item.edits == {'actor[0].tag.tag': 'Alice'}

    def test_performer_without_image_gets_no_thumb_key(self):
        item = _FakePlexItem()
        _worker()._sync_performers(
            item,
            {'performers': ['Alice', 'Bob'], 'performer_images': {'Alice': IMAGE}},
            _FakeResult(), False,
        )

        assert item.edits['actor[1].tag.tag'] == 'Bob'
        assert 'actor[1].tag.thumb' not in item.edits

    def test_thumb_lookup_survives_name_sanitisation(self):
        """Names are sanitised before reaching Plex; the map must follow."""
        from validation.sanitizers import sanitize_for_plex

        raw = 'Alice​ Smith'
        clean = sanitize_for_plex(raw, max_length=255)
        item = _FakePlexItem()
        _worker()._sync_performers(
            item,
            {'performers': [raw], 'performer_images': {raw: IMAGE}},
            _FakeResult(), False,
        )

        assert item.edits['actor[0].tag.tag'] == clean
        assert 'actor[0].tag.thumb' in item.edits
