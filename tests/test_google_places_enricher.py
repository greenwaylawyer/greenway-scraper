"""Unit tests for GooglePlacesEnricher and BaseEnricher candidate scoring/filtering."""

import unittest
from scrapers.enrichers.base_enricher import BaseEnricher, DiscoveryCandidate
from scrapers.enrichers.google_places import GooglePlacesEnricher


class TestGooglePlacesAndBaseEnricher(unittest.TestCase):
    def test_rejects_unrelated_city_and_state_only_google_places_candidates(self):
        """Verify unrelated law firms in the same city/state are rejected by the identity gate."""
        enricher = GooglePlacesEnricher()
        lawyer = {
            "full_name": "Meighan Colleen Lenihan Vargas",
            "first_name": None,
            "last_name": None,
            "city": "Norcross",
            "state": "GA",
            "license_state": "GA",
            "address": "",
        }

        unrelated_places = [
            {
                "place_id": "ChIJ5fNl4vej9YgRCBjzCpE5x9U",
                "name": "MLG-McLendon Law Group, LLC",
                "formatted_address": "5950 Live Oak Pkwy #140, Norcross, GA 30093",
                "geometry": {"location": {"lat": 33.9112382, "lng": -84.199018}},
            },
            {
                "place_id": "ChIJVVWF97-m9YgRHRpfWomRqXs",
                "name": "Nguyen & Pham, LLC",
                "formatted_address": "1805 Shackleford Ct Ste 100, Norcross, GA 30093",
                "geometry": {"location": {"lat": 33.9374634, "lng": -84.1540871}},
            },
            {
                "place_id": "ChIJZ3_vhMem9YgRUw3Jr2ijn3c",
                "name": "Law Office of Heena N. Kampani",
                "formatted_address": "5675 Jimmy Carter Blvd #670a, Norcross, GA 30071",
            },
            {
                "place_id": "ChIJRxNxqqqj9YgRRQZhH3wJtig",
                "name": "Kang C Lee PC Law Office",
                "formatted_address": "Kang C Lee PC, 3985 Steve Reynolds Blvd Building E, Norcross, GA 30093",
            },
            {
                "place_id": "ChIJ9Xn1x3YJ9YgRxFWuTNeq1IM",
                "name": "The Mager Law Firm, LLC",
                "formatted_address": "3295 River Exchange Dr Ste 374, Norcross, GA 30092",
            },
        ]

        for place in unrelated_places:
            self.assertIsNone(enricher._score_place_candidate(place, lawyer))

    def test_scores_matching_google_place_with_first_and_last_name_when_middle_names_present(self):
        enricher = GooglePlacesEnricher()
        lawyer = {
            "full_name": "Meighan Colleen Lenihan Vargas",
            "city": "Norcross",
            "state": "GA",
        }

        cand = enricher._score_place_candidate(
            {
                "place_id": "ChIJ_valid_vargas_place_id_123456",
                "name": "Law Office of Meighan Vargas, LLC",
                "formatted_address": "100 Technology Pkwy, Norcross, GA 30092",
                "geometry": {"location": {"lat": 33.95, "lng": -84.21}},
            },
            lawyer,
        )

        self.assertIsNotNone(cand)
        self.assertAlmostEqual(cand.confidence, 0.65)
        self.assertIn("name_match", cand.match_signals)
        self.assertIn("city_in_address", cand.match_signals)
        self.assertIn("state_in_address", cand.match_signals)

    def test_scores_firm_name_and_street_address_on_google_places(self):
        enricher = GooglePlacesEnricher()
        lawyer = {
            "full_name": "Robert McLendon",
            "firm_name": "McLendon Law Group, LLC",
            "city": "Norcross",
            "state": "GA",
            "address": "5950 Live Oak Parkway Suite 140",
        }

        cand = enricher._score_place_candidate(
            {
                "place_id": "ChIJ5fNl4vej9YgRCBjzCpE5x9U",
                "name": "MLG-McLendon Law Group, LLC",
                "formatted_address": "5950 Live Oak Pkwy #140, Norcross, GA 30093",
            },
            lawyer,
        )

        self.assertIsNotNone(cand)
        self.assertGreaterEqual(cand.confidence, 0.95)
        self.assertIn("firm_name_match", cand.match_signals)

    def test_base_enricher_directory_scoring_and_identity_gate(self):
        lawyer = {
            "full_name": "Meighan Colleen Lenihan Vargas",
            "first_name": None,
            "last_name": None,
            "city": "Norcross",
            "license_state": "GA",
        }

        unrelated = DiscoveryCandidate(
            url="https://www.avvo.com/attorneys/heena-kampani.html",
            name="Heena N. Kampani",
            location="Norcross, GA",
        )
        self.assertEqual(BaseEnricher.score_candidate(unrelated, lawyer), 0.0)

        different_first = DiscoveryCandidate(
            url="https://www.avvo.com/attorneys/carlos-vargas.html",
            name="Carlos Vargas",
            location="Norcross, GA",
        )
        self.assertEqual(BaseEnricher.score_candidate(different_first, lawyer), 0.0)

        matching = DiscoveryCandidate(
            url="https://www.avvo.com/attorneys/meighan-vargas.html",
            name="Meighan Vargas",
            location="Norcross, GA",
        )
        self.assertAlmostEqual(BaseEnricher.score_candidate(matching, lawyer), 1.0)

    def test_builds_unquoted_google_search_queries_with_first_last_fallback(self):
        lawyer = {
            "full_name": "Meighan Colleen Lenihan Vargas",
            "city": "Norcross",
            "state": "GA",
        }
        queries = GooglePlacesEnricher._build_search_queries(lawyer)
        self.assertEqual(
            queries,
            [
                "Meighan Colleen Lenihan Vargas Norcross GA attorney",
                "Meighan Vargas Norcross GA attorney",
            ],
        )


if __name__ == "__main__":
    unittest.main()
