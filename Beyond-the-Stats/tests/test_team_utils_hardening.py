"""Unit tests for team_utils error handling with unhashable types (dict)."""

import os
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_WEBSITE = _ROOT / "Website"
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
if str(_WEBSITE) not in sys.path:
    sys.path.insert(0, str(_WEBSITE))

from team_utils import (
    _team_name_for_display,
    _team_name_for_db,
    _normalize_team_key,
)


class TestTeamUtilsUnhashableTypes(unittest.TestCase):
    def test_team_name_for_display_with_dict(self):
        # A dictionary object passed instead of a string
        dict_team = {"name": "Arsenal", "id": 101}
        result = _team_name_for_display(dict_team)
        self.assertEqual(result, "Arsenal")

        dict_team2 = {"team": "Chelsea"}
        result2 = _team_name_for_display(dict_team2)
        self.assertEqual(result2, "Chelsea")

        dict_team3 = {"team_name": "Liverpool"}
        result3 = _team_name_for_display(dict_team3)
        self.assertEqual(result3, "Liverpool")

        # Empty dict should return empty string without error
        self.assertEqual(_team_name_for_display({}), "")

    def test_team_name_for_display_with_set_comprehension(self):
        # Ensure sets containing dicts converted via _team_name_for_display never raise TypeError: unhashable type: 'dict'
        raw_list = [
            {"name": "Arsenal"},
            "Chelsea",
            {"team": "Liverpool"},
            "Arsenal",
        ]
        unique_teams = {_team_name_for_display(t) for t in raw_list}
        self.assertIn("Arsenal", unique_teams)
        self.assertIn("Chelsea", unique_teams)
        self.assertIn("Liverpool", unique_teams)
        self.assertEqual(len(unique_teams), 3)

    def test_team_name_for_db_with_dict(self):
        dict_team = {"display_name": "Arsenal FC"}
        result = _team_name_for_db(dict_team)
        self.assertEqual(result, "Arsenal FC")

    def test_normalize_team_key_with_dict(self):
        dict_team = {"team": "Real Madrid"}
        result = _normalize_team_key(dict_team)
        self.assertEqual(result, "real madrid")


if __name__ == "__main__":
    unittest.main()
