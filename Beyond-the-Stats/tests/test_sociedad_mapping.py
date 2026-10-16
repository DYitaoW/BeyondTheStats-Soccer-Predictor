"""Unit tests verifying Real Sociedad vs Real Sociedad B mapping separation across leagues and competitions."""

import json
import sys
import unittest
from pathlib import Path

# Setup paths
_ROOT = Path(__file__).resolve().parent.parent
_SHARED = _ROOT / "shared"
_WEBSITE = _ROOT / "Website"
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_SHARED) not in sys.path:
    sys.path.insert(0, str(_SHARED))
if str(_WEBSITE) not in sys.path:
    sys.path.insert(0, str(_WEBSITE))

from team_mapping_groups import (
    normalize_team_key,
    fuzzy_resolve_team_name,
    lookup_mapped_name,
    candidate_teams_for_competition,
    store_team_mapping,
)
from competition_rules import canonical_team_name


class TestRealSociedadMapping(unittest.TestCase):
    def setUp(self):
        # Sample master mapping dictionary structure mimicking team_name_mapping_master.json
        self.mapping = {
            "Spain/La Liga": {
                "Real Sociedad": "Sociedad",
                "Real Sociedad de Fútbol": "Sociedad",
                "Real Sociedad de Futbol": "Sociedad",
                "R. Sociedad": "Sociedad",
            },
            "Spain/La Liga 2": {
                "Real Sociedad B": "Sociedad B",
                "Real Sociedad de Fútbol B": "Sociedad B",
                "Real Sociedad de Futbol B": "Sociedad B",
                "R. Sociedad B": "Sociedad B",
                "Sanse": "Sociedad B",
            },
            "Spain/Copa del Rey": {
                "Real Sociedad": "Sociedad",
                "Real Sociedad de Fútbol": "Sociedad",
            },
            "Europe/Champions League": {
                "Real Sociedad": "Sociedad",
            },
            "Europe/Europa League": {
                "Real Sociedad": "Sociedad",
            },
            "Europe/Conference League": {
                "Real Sociedad": "Sociedad",
            },
        }

    def test_normalize_team_key_sociedad(self):
        """Ensure 'sociedad' is preserved and not stripped as a stop word."""
        norm_sociedad = normalize_team_key("Real Sociedad")
        norm_sociedad_b = normalize_team_key("Real Sociedad B")
        self.assertIn("sociedad", norm_sociedad)
        self.assertIn("sociedad", norm_sociedad_b)
        self.assertNotEqual(norm_sociedad, "")
        self.assertNotEqual(norm_sociedad_b, "b")
        self.assertEqual(norm_sociedad_b, "sociedad b")

    def test_la_liga_resolves_to_sociedad(self):
        """In Spain/La Liga, Real Sociedad and its variations resolve to senior Sociedad."""
        self.assertEqual(lookup_mapped_name("Real Sociedad", "Spain/La Liga", self.mapping), "Sociedad")
        self.assertEqual(lookup_mapped_name("Real Sociedad de Fútbol", "Spain/La Liga", self.mapping), "Sociedad")
        self.assertEqual(lookup_mapped_name("R. Sociedad", "Spain/La Liga", self.mapping), "Sociedad")

    def test_la_liga_2_resolves_to_sociedad_b(self):
        """In Spain/La Liga 2, Real Sociedad and Real Sociedad B resolve to Sociedad B."""
        self.assertEqual(lookup_mapped_name("Real Sociedad B", "Spain/La Liga 2", self.mapping), "Sociedad B")
        self.assertEqual(lookup_mapped_name("Real Sociedad", "Spain/La Liga 2", self.mapping), "Sociedad B")
        self.assertEqual(lookup_mapped_name("Real Sociedad de Fútbol B", "Spain/La Liga 2", self.mapping), "Sociedad B")
        self.assertEqual(lookup_mapped_name("Sanse", "Spain/La Liga 2", self.mapping), "Sociedad B")

    def test_copa_del_rey_resolves_to_sociedad(self):
        """In Copa del Rey, Real Sociedad maps to Sociedad."""
        self.assertEqual(lookup_mapped_name("Real Sociedad", "Spain/Copa del Rey", self.mapping), "Sociedad")

    def test_uefa_competitions_resolve_to_sociedad(self):
        """In Champions League, Europa League, Conference League, Real Sociedad maps to Sociedad."""
        self.assertEqual(lookup_mapped_name("Real Sociedad", "Europe/Champions League", self.mapping), "Sociedad")
        self.assertEqual(lookup_mapped_name("Real Sociedad", "Europe/Europa League", self.mapping), "Sociedad")
        self.assertEqual(lookup_mapped_name("Real Sociedad", "Europe/Conference League", self.mapping), "Sociedad")

    def test_fuzzy_resolution_disambiguation(self):
        """Fuzzy match should distinguish Real Sociedad from Real Sociedad B properly."""
        candidates = ["Sociedad", "Sociedad B", "Real Madrid", "Barcelona"]
        # Real Sociedad -> Sociedad
        self.assertEqual(fuzzy_resolve_team_name("Real Sociedad", candidates), "Sociedad")
        # Real Sociedad B / Sanse / Real Sociedad II -> Sociedad B
        self.assertEqual(fuzzy_resolve_team_name("Real Sociedad B", candidates), "Sociedad B")
        self.assertEqual(fuzzy_resolve_team_name("Real Sociedad II", candidates), "Sociedad B")

    def test_short_substring_does_not_falsely_match_sociedad_b(self):
        """Teams containing 'b' or short substrings should NOT falsely match Sociedad B."""
        candidates = ["Sociedad", "Sociedad B"]
        # Random clubs previously misassigned because of 'b'
        for bogus_team in ["Sabah FK", "Slovan Bratislava", "Dinamo Zagreb", "FC København", "Jablonec", "Iberia 1999"]:
            resolved = fuzzy_resolve_team_name(bogus_team, candidates)
            self.assertNotEqual(
                resolved,
                "Sociedad B",
                f"{bogus_team} should not resolve to Sociedad B"
            )

    def test_candidate_teams_for_competition_filters_reserves(self):
        """Candidate teams for top flight and UEFA competitions must exclude reserve teams."""
        context = {
            "teams": ["Sociedad", "Sociedad B", "Real Madrid", "Barcelona", "Athletic Club B"]
        }
        ucl_candidates = candidate_teams_for_competition("Europe/Champions League", context)
        self.assertIn("Sociedad", ucl_candidates)
        self.assertNotIn("Sociedad B", ucl_candidates)
        self.assertNotIn("Athletic Club B", ucl_candidates)

        la_liga_candidates = candidate_teams_for_competition("Spain/La Liga", context)
        self.assertIn("Sociedad", la_liga_candidates)
        self.assertNotIn("Sociedad B", la_liga_candidates)

        la_liga_2_candidates = candidate_teams_for_competition("Spain/La Liga 2", context)
        self.assertIn("Sociedad B", la_liga_2_candidates)
        self.assertNotIn("Sociedad", la_liga_2_candidates)

    def test_canonical_team_name_rules(self):
        """Website competition_rules canonical_team_name handles division and competition separation."""
        self.assertEqual(canonical_team_name("Spain/La Liga", "Real Sociedad"), "Sociedad")
        self.assertEqual(canonical_team_name("Spain/La Liga", "Sociedad"), "Sociedad")
        self.assertEqual(canonical_team_name("Europe/Europa League", "Real Sociedad"), "Sociedad")
        self.assertEqual(canonical_team_name("Spain/La Liga 2", "Real Sociedad"), "Sociedad B")
        self.assertEqual(canonical_team_name("Spain/La Liga 2", "Real Sociedad B"), "Sociedad B")
        self.assertEqual(canonical_team_name("Spain/La Liga 2", "Sociedad B"), "Sociedad B")

    def test_store_team_mapping_reserves_guard(self):
        """store_team_mapping should prevent reserve teams from leaking into La Liga or UEFA."""
        mapping = {}
        store_team_mapping("Spain/La Liga", "Real Sociedad B", "Sociedad B", mapping)
        self.assertNotIn("Spain/La Liga", mapping)

        store_team_mapping("Europe/Champions League", "Real Sociedad B", "Sociedad B", mapping)
        self.assertNotIn("Europe/Champions League", mapping)

        # But allowed in La Liga 2
        store_team_mapping("Spain/La Liga 2", "Real Sociedad B", "Sociedad B", mapping)
        self.assertEqual(mapping.get("Spain/La Liga 2", {}).get("Real Sociedad B"), "Sociedad B")


if __name__ == "__main__":
    unittest.main()

