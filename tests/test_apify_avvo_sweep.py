"""Unit tests for the Avvo listing-page parsing used by the paged discover sweep."""

from workers.apify_bulk_worker import avvo_id_from_url, parse_listing_links

BASE = "https://www.avvo.com/immigration-lawyer/ca.html"


def test_parse_listing_links_extracts_profiles_and_last_page():
    links = [
        "https://www.avvo.com/attorneys/90064-ca-layla-khamoushian-75849.html?pa=95",
        "https://www.avvo.com/attorneys/90064-ca-layla-khamoushian-75849.html?pa=95#reviews",
        "https://www.avvo.com/saved_content/toggle_save_lawyer?lawyer_id=75849",
        "https://www.avvo.com/attorneys/30309-ga-h-fogle-447539.html?pa=95",
        "https://www.avvo.com/immigration-lawyer/ca/anaheim.html",
        f"{BASE}?page=2&sort=client_rating",
        f"{BASE}?page=2",
        f"{BASE}?page=179",
        "https://www.avvo.com/criminal-defense-lawyer/ca.html?page=900",
    ]

    profiles, last_page = parse_listing_links(links, BASE)

    assert profiles == {
        "75849": "https://www.avvo.com/attorneys/90064-ca-layla-khamoushian-75849.html",
        "447539": "https://www.avvo.com/attorneys/30309-ga-h-fogle-447539.html",
    }
    assert last_page == 179


def test_parse_listing_links_empty_page():
    profiles, last_page = parse_listing_links(["https://www.avvo.com/find-a-lawyer"], BASE)

    assert profiles == {}
    assert last_page is None


def test_avvo_id_from_url():
    assert avvo_id_from_url("90064-ca-layla-khamoushian-75849.html") == "75849"
    assert avvo_id_from_url("https://www.avvo.com/attorneys/92801-ca-fady-eskandar-4738601.html?pa=95") == "4738601"
    assert avvo_id_from_url("https://www.avvo.com/find-a-lawyer") is None
    assert avvo_id_from_url(None) is None
