"""Avvo via apify/web-fetch: page parsing, candidate matching and worker flows.

Synthetic HTML mirrors the structure of real Avvo pages (Oct 2026): profile
pages carry a LocalBusiness JSON-LD block with the headshot; search pages carry
one Person JSON-LD block per ``.serp-card.organic-card``. No network or DB.
"""

import json

from scrapers.enrichers.apify_avvo import ApifyAvvoEnricher, avvo_id_from_url
from scrapers.enrichers.avvo import AvvoEnricher, is_placeholder_photo
from utils.apify_client import ApifyClient, WEB_FETCH_ACTOR
from workers.apify_bulk_worker import ApifyBulkWorker

PROFILE_URL = "https://www.avvo.com/attorneys/32801-fl-jane-sample-1234567.html"
PHOTO = "https://images.avvo.com/avvo/ugc/images/head_shot/standard/lawyer_jane_sample_1234567_1.jpg"
NO_PHOTO = "https://images.avvo.com/avvo/cms/images/amos_assets/no-photo-v1-112x140.png"


def profile_html(image=PHOTO, name="Jane Sample"):
    ld = {
        "@context": "https://schema.org",
        "@type": "LocalBusiness",
        "name": name,
        "description": "Immigration attorney in Orlando.",
        "image": {"@type": "ImageObject", "url": image, "name": name},
        "telephone": "Not Available",
        "sameAs": ["https://www.linkedin.com/in/janesample/"],
        "address": {
            "@type": "PostalAddress",
            "streetAddress": "1 Main Street",
            "addressLocality": "Orlando",
            "addressRegion": "FL",
            "postalCode": "32801",
            "addressCountry": "US",
        },
        "knowsAbout": ["Immigration", "Family"],
    }
    return f'<html><head><script type="application/ld+json">{json.dumps(ld)}</script></head><body></body></html>'


def search_card(name, city, state, url, image=PHOTO):
    ld = {
        "@type": "Person",
        "name": name,
        "image": {"url": image},
        "worksFor": {"address": {"addressLocality": city, "addressRegion": state}, "telephone": ""},
    }
    return (
        f'<div class="serp-card organic-card"><a href="{url}">View profile</a>'
        f'<script type="application/ld+json">{json.dumps(ld)}</script></div>'
    )


SEARCH_HTML = "<html><body>" + search_card(
    "Jane Sample", "Orlando", "FL", PROFILE_URL
) + search_card(
    "Janet Other", "Miami", "FL", "https://www.avvo.com/attorneys/33101-fl-janet-other-7654321.html"
) + "</body></html>"


# ── parsing ──────────────────────────────────────────────────────────────────

def test_parse_profile_html_returns_photo_and_core_fields():
    data = AvvoEnricher.parse_profile_html(profile_html(), PROFILE_URL)

    assert data["full_name"] == "Jane Sample"
    assert data["photo_url"] == PHOTO
    assert data["address"]["city"] == "Orlando"
    assert data["address"]["state"] == "FL"
    assert data["practice_areas"] == ["Immigration", "Family"]
    assert data["social_links"] == {"linkedin": "https://www.linkedin.com/in/janesample/"}
    assert data["source_profile_url"] == PROFILE_URL
    assert "phone" not in data  # "Not Available" is dropped


def test_parse_profile_html_drops_placeholder_photo():
    data = AvvoEnricher.parse_profile_html(profile_html(image=NO_PHOTO), PROFILE_URL)

    assert data["full_name"] == "Jane Sample"
    assert "photo_url" not in data


def test_parse_profile_html_returns_none_for_block_page():
    assert AvvoEnricher.parse_profile_html("<html><title>Just a moment...</title></html>", PROFILE_URL) is None


def test_is_placeholder_photo():
    assert is_placeholder_photo(NO_PHOTO)
    assert not is_placeholder_photo(PHOTO)
    assert not is_placeholder_photo(None)


def test_avvo_id_from_url():
    assert avvo_id_from_url(PROFILE_URL) == "1234567"
    assert avvo_id_from_url("https://www.avvo.com/search") is None


# ── Apify client ─────────────────────────────────────────────────────────────

class FakeClient(ApifyClient):
    def __init__(self, item, status="SUCCEEDED"):
        self.token = "test"
        self.item, self.status, self.calls = item, status, []

    async def run_actor(self, actor_id, input_data, **kw):
        self.calls.append((actor_id, input_data, kw))
        return "run1"

    async def wait_for_finish(self, run_id, **kw):
        return self.status

    async def get_dataset_items(self, run_id, offset=0, limit=1000):
        return [self.item] if self.item else []


async def test_fetch_html_passes_timeout_to_apify_and_returns_raw():
    client = FakeClient({"raw": "<html>ok</html>", "fetch": {"httpStatusCode": 200}})

    assert await client.fetch_html(PROFILE_URL, timeout_seconds=90) == "<html>ok</html>"
    actor_id, input_data, kw = client.calls[0]
    assert actor_id == WEB_FETCH_ACTOR
    assert input_data == {"url": PROFILE_URL, "formats": ["raw"]}
    assert kw["timeout_seconds"] == 90  # Apify kills the run itself; no 1h runs


async def test_fetch_html_returns_none_on_http_error_or_failed_run():
    assert await FakeClient({"raw": "<html>", "fetch": {"httpStatusCode": 403}}).fetch_html(PROFILE_URL) is None
    assert await FakeClient({"raw": "<html>"}, status="TIMED-OUT").fetch_html(PROFILE_URL) is None


# ── enricher (single-profile path) ───────────────────────────────────────────

class FakePageClient:
    def __init__(self, pages):
        self.pages = pages

    async def fetch_html(self, url, **kw):
        if "search" in url:
            return self.pages.get("search")
        return self.pages.get(url)


def make_enricher(pages):
    enricher = ApifyAvvoEnricher.__new__(ApifyAvvoEnricher)
    enricher.client = FakePageClient(pages)
    return enricher


async def test_enricher_discover_profile_ranks_matching_card_first():
    enricher = make_enricher({"search": SEARCH_HTML})
    lawyer = {"full_name": "Jane Sample", "first_name": "Jane", "last_name": "Sample", "city": "Orlando", "state": "FL"}

    candidates = await enricher.discover_profile(lawyer)

    assert candidates[0].url == PROFILE_URL
    assert candidates[0].confidence > candidates[1].confidence


async def test_enricher_parse_profile_adds_avvo_id_and_photo():
    enricher = make_enricher({PROFILE_URL: profile_html()})

    data = await enricher.parse_profile(PROFILE_URL, {})

    assert data["photo_url"] == PHOTO
    assert data["avvo_id"] == "1234567"


# ── bulk worker flows ────────────────────────────────────────────────────────

def make_worker(pages, mode="enrich"):
    worker = ApifyBulkWorker.__new__(ApifyBulkWorker)
    worker.mode, worker.dry_run, worker.limit, worker.batch_size = mode, False, None, 100
    worker.source_key, worker._on_apify_status, worker._batch_id = "avvo", None, None
    worker.client = FakePageClient(pages)
    worker.stats = {k: 0 for k in (
        "selected", "queries", "items", "matched", "unmatched", "inserted", "merged", "errors",
        "listing_pages", "listings_seen", "already_known", "new_profiles_queued",
        "profile_fetch_failed", "photos_found",
    )}
    worker._config = lambda: {}
    worker.persisted = []
    worker._persist_match = lambda target, mapped: worker.persisted.append((target["id"], mapped))
    return worker


TARGET = {"id": 7, "full_name": "Jane Sample", "first_name": "Jane", "last_name": "Sample",
          "city": "Orlando", "state": "FL", "merged_data": {}}


async def test_enrich_matches_search_card_then_merges_profile_with_photo():
    worker = make_worker({"search": SEARCH_HTML, PROFILE_URL: profile_html()})
    worker.select_targets = lambda: [TARGET]

    stats = await worker._run_enrich()

    assert stats["matched"] == 1 and stats["merged"] == 1
    target_id, mapped = worker.persisted[0]
    assert target_id == 7
    assert mapped["photo_url"] == PHOTO
    assert mapped["source_profile_url"] == PROFILE_URL


async def test_enrich_skips_target_without_a_confident_match():
    worker = make_worker({"search": SEARCH_HTML})
    worker.select_targets = lambda: [{**TARGET, "full_name": "Zed Nobody", "first_name": "Zed", "last_name": "Nobody"}]

    stats = await worker._run_enrich()

    assert stats["unmatched"] == 1 and stats["merged"] == 0
    assert worker.persisted == []


async def test_refresh_fills_photo_for_existing_avvo_profiles():
    worker = make_worker({PROFILE_URL: profile_html()}, mode="refresh")
    worker.select_refresh_targets = lambda: [{**TARGET, "avvo_url": PROFILE_URL}]

    stats = await worker.run()

    assert stats["merged"] == 1 and stats["photos_found"] == 1
    assert worker.persisted[0][1]["photo_url"] == PHOTO


async def test_refresh_counts_failed_fetches():
    worker = make_worker({}, mode="refresh")  # page fetch returns None
    worker.select_refresh_targets = lambda: [{**TARGET, "avvo_url": PROFILE_URL}]

    stats = await worker.run()

    assert stats["profile_fetch_failed"] == 1 and stats["merged"] == 0
