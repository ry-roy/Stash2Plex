"""
Unit tests for Thumbor-backed performer thumbnails.

Tests verify:
- The Thumbor base URL acts as the feature switch
- The whole source URL is percent-encoded into one path segment
- Origin rewriting preserves the path and query string
- A configured transform replaces the default crop, and a bad one falls back
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


FACE_ZOOM = "400x400/smart/filters:face_zoom(0.5)"


class TestTransform:
    """The crop is configurable; the default must survive a bad value."""

    def test_configured_transform_replaces_the_default(self):
        url = performer_thumb_url(IMAGE, THUMBOR, transform=FACE_ZOOM)

        assert url.startswith(f"{THUMBOR}/unsafe/{FACE_ZOOM}/")
        assert "300x300" not in url

    def test_source_stays_one_segment_under_a_filter_chain(self):
        """filters: chains contain ( ) : — none may split the source segment."""
        url = performer_thumb_url(IMAGE, THUMBOR, transform=FACE_ZOOM)

        assert url.split(f"{FACE_ZOOM}/", 1)[1].count("/") == 0

    @pytest.mark.parametrize("transform", [None, "", "   ", "/"])
    def test_empty_transform_falls_back_to_the_default(self, transform):
        assert performer_thumb_url(IMAGE, THUMBOR, transform=transform) == \
            performer_thumb_url(IMAGE, THUMBOR)

    def test_surrounding_slashes_do_not_double_up(self):
        url = performer_thumb_url(IMAGE, THUMBOR, transform="/400x400/smart/")

        assert "/unsafe/400x400/smart/http" in url
        assert "//" not in url.split("://", 1)[1]

    def test_transform_composes_with_source_base(self):
        url = performer_thumb_url(IMAGE, THUMBOR, "http://stash:9999", FACE_ZOOM)

        assert f"/unsafe/{FACE_ZOOM}/" in url
        assert "http%3A%2F%2Fstash%3A9999" in url


class TestTransformValidation:
    """A bad setting must cost the custom crop, not the whole feature."""

    @staticmethod
    def _normalized(value):
        from validation.config import Stash2PlexConfig

        config = Stash2PlexConfig(
            plex_url="http://localhost:32400",
            plex_token="x" * 24,
            performer_thumb_transform=value,
        )
        return config.performer_thumb_transform

    def test_valid_transform_is_kept(self):
        assert self._normalized(FACE_ZOOM) == FACE_ZOOM

    def test_unsafe_prefix_is_stripped(self):
        """thumbor.py owns the prefix; a user pasting a full path is fine."""
        assert self._normalized(f"/unsafe/{FACE_ZOOM}/") == FACE_ZOOM

    @pytest.mark.parametrize("value", [
        None, "", "   ",
        "../../etc/passwd",       # traversal out of the transform segment
        "400x400/smart evil",     # whitespace would break the URL
        "400x400/smart?x=1",      # query would be read as part of the source
        "400x400/smart#frag",
    ])
    def test_unusable_values_fall_back_to_the_default(self, value):
        assert self._normalized(value) is None

    def test_rejected_transform_still_yields_the_default_url(self):
        """End to end: the feature keeps working after a bad setting."""
        url = performer_thumb_url(IMAGE, THUMBOR, transform=self._normalized(".."))

        assert "/unsafe/300x300/smart/" in url


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


def _worker(thumbor_base=THUMBOR, source_base=None, transform=None):
    """A SyncWorker with only the config _sync_performers touches."""
    from worker.processor import SyncWorker

    worker = SyncWorker.__new__(SyncWorker)
    worker.config = type('Config', (), {
        'thumbor_base_url': thumbor_base,
        'performer_image_source_base_url': source_base,
        'performer_thumb_transform': transform,
    })()
    return worker


class TestSyncPerformers:
    """Tests for the Plex actor edit."""

    def test_thumb_accompanies_the_name_edit(self):
        item = _FakePlexItem()
        needs_reload = _worker()._sync_performers(
            item,
            {'performers': ['Alice'], 'performer_images': {'Alice': IMAGE}},
            _FakeResult(), False, [],
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
            _FakeResult(), False, [],
        )

        assert needs_reload is True
        assert item.edits['actor[0].tag.thumb'].startswith(THUMBOR)

    def test_no_edit_when_names_match_and_no_images(self):
        """Without images this must stay a no-op, exactly as before."""
        item = _FakePlexItem(actors=['Alice'])
        needs_reload = _worker()._sync_performers(
            item, {'performers': ['Alice']}, _FakeResult(), False, [],
        )

        assert needs_reload is False
        assert item.edits is None

    def test_unconfigured_thumbor_sends_names_only(self):
        item = _FakePlexItem()
        _worker(thumbor_base=None)._sync_performers(
            item,
            {'performers': ['Alice'], 'performer_images': {'Alice': IMAGE}},
            _FakeResult(), False, [],
        )

        assert item.edits == {'actor[0].tag.tag': 'Alice', 'actor.locked': 1}

    def test_performer_without_image_gets_no_thumb_key(self):
        item = _FakePlexItem()
        _worker()._sync_performers(
            item,
            {'performers': ['Alice', 'Bob'], 'performer_images': {'Alice': IMAGE}},
            _FakeResult(), False, [],
        )

        assert item.edits['actor[1].tag.tag'] == 'Bob'
        assert 'actor[1].tag.thumb' not in item.edits

    def test_configured_transform_reaches_the_edit(self):
        """The setting must actually be threaded through, not just accepted."""
        item = _FakePlexItem()
        _worker(transform=FACE_ZOOM)._sync_performers(
            item,
            {'performers': ['Alice'], 'performer_images': {'Alice': IMAGE}},
            _FakeResult(), False, [],
        )

        assert f"/unsafe/{FACE_ZOOM}/" in item.edits['actor[0].tag.thumb']

    def test_config_without_the_field_uses_the_default(self):
        """Older configs predate the setting; getattr must not explode."""
        item = _FakePlexItem()
        worker = _worker()
        del type(worker.config).performer_thumb_transform

        worker._sync_performers(
            item,
            {'performers': ['Alice'], 'performer_images': {'Alice': IMAGE}},
            _FakeResult(), False, [],
        )

        assert "/unsafe/300x300/smart/" in item.edits['actor[0].tag.thumb']

    def test_thumb_lookup_survives_name_sanitisation(self):
        """Names are sanitised before reaching Plex; the map must follow."""
        from validation.sanitizers import sanitize_for_plex

        raw = 'Alice​ Smith'
        clean = sanitize_for_plex(raw, max_length=255)
        item = _FakePlexItem()
        _worker()._sync_performers(
            item,
            {'performers': [raw], 'performer_images': {raw: IMAGE}},
            _FakeResult(), False, [],
        )

        assert item.edits['actor[0].tag.tag'] == clean
        assert 'actor[0].tag.thumb' in item.edits

    def test_new_performer_with_thumb_is_locked_and_awaits_verification(self):
        """Success for a new name is only claimed after the post-reload check."""
        item, result, pending = _FakePlexItem(), _FakeResult(), []
        _worker()._sync_performers(
            item,
            {'performers': ['Alice'], 'performer_images': {'Alice': IMAGE}},
            result, False, pending,
        )

        assert item.edits['actor.locked'] == 1
        assert [p['expected'] for p in pending] == [['Alice']]
        assert result.successes == []

    def test_thumb_only_edit_succeeds_without_verification(self):
        item, result, pending = _FakePlexItem(actors=['Alice']), _FakeResult(), []
        _worker()._sync_performers(
            item,
            {'performers': ['Alice'], 'performer_images': {'Alice': IMAGE}},
            result, False, pending,
        )

        assert pending == []
        assert result.successes == ['performers']


class TestHookPath:
    """The scene-update hook must not drop artwork on its way to the queue."""

    def test_hook_job_keeps_performer_images_and_poster(self, mocker):
        from hooks.handlers import on_scene_update

        stash = mocker.Mock()
        stash.call_GQL.return_value = {'findScene': {
            'id': '1',
            'title': 'Scene',
            'files': [{'path': '/media/scene.mp4'}],
            'performers': [{'name': 'Alice', 'image_path': IMAGE}],
            'paths': {'screenshot': 'http://127.0.0.1:9999/scene/1/screenshot'},
        }}
        queue = mocker.Mock()

        on_scene_update(
            1, {'studio_id': '2'}, queue, stash=stash, scan_already_checked=True,
        )

        job = queue.try_enqueue.call_args.args[2]
        assert job['performer_images'] == {'Alice': IMAGE}
        assert job['poster_url'] == 'http://127.0.0.1:9999/scene/1/screenshot'
