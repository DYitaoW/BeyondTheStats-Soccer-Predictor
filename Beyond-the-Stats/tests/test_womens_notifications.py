"""Women's teams in notification targeting, plus push-to-start / token-prune fixes.

Women's sides share bare club names with men's teams ("Arsenal", "Aston Villa",
"Birmingham" in the WSL). They used to resolve to the men's team id, so a
men's-club follower got the women's game alerts while women's-club followers
(following the catalog id, e.g. ``arsenal-women``) got nothing. NWSL ESPN
spellings ("Portland Thorns FC", "Gotham FC") also missed catalog ids.

Also covers:
- one owner per push-to-start token (rotated device tokens caused duplicate
  Live Activities on one phone);
- token auto-prune for long (160-hex) tokens only after BOTH environments
  reject it, never after a single forced-environment rejection.

Kickoff times are relative to wall-clock time (no hardcoded dates).
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
WEBSITE_DIR = ROOT / "Website"
for p in (ROOT, WEBSITE_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import config
import live_poller as lp
import notifications as n
from shared import sqlite_store
from standardized_teams import (
    has_womens_team_marker,
    is_womens_competition,
    resolve_team_info,
)

WSL = "England/Women's Super League"
NWSL = "North America/NWSL"
PL = "England/Premier League"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Base(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "womens.db"
        sqlite_store.ensure_store(self.db_path)
        self._orig_default_db = sqlite_store._default_db_path
        sqlite_store._default_db_path = lambda: self.db_path
        self._orig_store = n.sqlite_store
        n.sqlite_store = sqlite_store
        self._orig_broadcast = config.APNS_BROADCAST_ENABLED
        config.APNS_BROADCAST_ENABLED = False
        self._reset()

    def tearDown(self):
        self._reset()
        config.APNS_BROADCAST_ENABLED = self._orig_broadcast
        n.sqlite_store = self._orig_store
        sqlite_store._default_db_path = self._orig_default_db
        self.temp_dir.cleanup()

    def _reset(self):
        with lp._live_scores_lock:
            lp._live_scores.clear()
        lp._la_timer_anchors.clear()
        for store in (
            n._apns_notification_queue, n._live_activities, n._match_notification_subscriptions,
            n._match_subscription_events, n._team_notification_subscriptions, n._team_subscription_events,
            n._team_follow_records, n._push_to_start_tokens, n._push_started, n._token_environment_cache,
            n.ios_device_tokens, n.device_tokens, n._device_la_settings, n._la_channels,
            n._channel_activity_devices,
        ):
            store.clear()


class WomensTeamIdentityTests(unittest.TestCase):
    def test_competition_and_name_detection(self):
        for comp in (WSL, NWSL, "United States/NWSL", "England/WSL", "eng.w.1", "usa.nwsl"):
            self.assertTrue(is_womens_competition(comp), comp)
        for comp in (PL, "United States/MLS", "eng.1", "Spain/La Liga", ""):
            self.assertFalse(is_womens_competition(comp), comp)
        self.assertTrue(has_womens_team_marker("Arsenal Women"))
        self.assertTrue(has_womens_team_marker("Arsenal WFC"))
        self.assertFalse(has_womens_team_marker("Arsenal"))
        self.assertFalse(has_womens_team_marker("Wolverhampton Wanderers"))

    def test_bare_wsl_names_never_resolve_to_mens_clubs(self):
        cases = {
            "Arsenal": "arsenal-women",
            "Arsenal Women": "arsenal-women",
            "Aston Villa": "aston-villa-women",
            "West Ham United": "west-ham-united-women",
            "Birmingham": "birmingham-city-women",
            "Charlton": "charlton-athletic-women",
            "Brighton & Hove Albion Women": "brighton-women",
            "London City Lionesses": "london-city-lionesses",
        }
        for name, expected in cases.items():
            self.assertEqual(resolve_team_info(name, WSL)["id"], expected, name)
        # ESPN slug as competition, and the "England/WSL" alias.
        self.assertEqual(resolve_team_info("Arsenal", "eng.w.1")["id"], "arsenal-women")
        self.assertEqual(resolve_team_info("Arsenal", "England/WSL")["id"], "arsenal-women")

    def test_mens_resolution_unchanged(self):
        self.assertEqual(resolve_team_info("Arsenal", PL)["id"], "arsenal")
        self.assertEqual(resolve_team_info("Arsenal", "")["id"], "arsenal")
        self.assertEqual(resolve_team_info("Aston Villa", PL)["id"], "aston-villa")
        self.assertEqual(resolve_team_info("Birmingham", "England/Championship")["id"], "birmingham-city")
        # A women's lookup must not leave a women's entry behind for the men's club.
        resolve_team_info("Charlton", WSL)
        self.assertNotIn("women", resolve_team_info("Charlton", "England/Championship")["id"])

    def test_nwsl_espn_spellings_match_catalog_ids(self):
        cases = {
            "Portland Thorns FC": "portland-thorns",
            "Racing Louisville FC": "racing-louisville",
            "San Diego Wave FC": "san-diego-wave",
            "Seattle Reign FC": "seattle-reign",
            "Gotham FC": "nj-ny-gotham-fc",
            "Chicago Stars FC": "chicago-red-stars",
            "Utah Royals FC": "utah-royals",
        }
        for name, expected in cases.items():
            self.assertEqual(resolve_team_info(name, NWSL)["id"], expected, name)
            self.assertEqual(resolve_team_info(name, "usa.nwsl")["id"], expected, name)

    def test_women_alias_without_competition(self):
        # An app subscribing with just a women's name (no competition / team_id).
        self.assertEqual(resolve_team_info("Chelsea FC Women")["id"], "chelsea-women")
        self.assertEqual(resolve_team_info("Arsenal WFC")["id"], "arsenal-women")

    def test_catalog_names_resolve_to_same_ids_as_live_names(self):
        # The catalog resolves its canonical display names; live games resolve
        # ESPN names inside the competition. Both must give the same id.
        pairs = [
            ("Arsenal Women", WSL, "Arsenal", WSL),
            ("Brighton Women", WSL, "Brighton & Hove Albion Women", WSL),
            ("Portland Thorns", NWSL, "Portland Thorns FC", NWSL),
            ("NJ/NY Gotham FC", NWSL, "Gotham FC", NWSL),
        ]
        for cat_name, cat_comp, live_name, live_comp in pairs:
            self.assertEqual(
                resolve_team_info(cat_name, cat_comp)["id"],
                resolve_team_info(live_name, live_comp)["id"],
                (cat_name, live_name),
            )


class WomensNotificationTargetingTests(_Base):
    def test_mens_follower_gets_no_womens_alerts_and_vice_versa(self):
        n.subscribe_team("mens-fan", "Arsenal", PL)
        n.subscribe_team("womens-fan", "", team_id="arsenal-women")
        n.subscribe_team("womens-fan-by-name", "Arsenal", WSL)

        # WSL game, both the bare name (pipeline rows) and the ESPN "Women" name.
        for home in ("Arsenal", "Arsenal Women"):
            subs = set(n.for_game_or_team_subscribers("w-1", WSL, home_team=home, away_team="Chelsea Women"))
            self.assertNotIn("mens-fan", subs, home)
            self.assertEqual(subs, {"womens-fan", "womens-fan-by-name"}, home)

        # Men's game reaches only the men's follower.
        subs = set(n.for_game_or_team_subscribers("m-1", PL, home_team="Arsenal", away_team="Chelsea"))
        self.assertEqual(subs, {"mens-fan"})

    def test_unmapped_bare_wsl_club_does_not_reach_mens_followers(self):
        n.subscribe_team("blues-fan", "Birmingham", "England/Championship")
        subs = n.for_game_or_team_subscribers("w-2", WSL, home_team="Aston Villa Women", away_team="Birmingham")
        self.assertNotIn("blues-fan", subs)

    def test_nwsl_follow_by_catalog_id_matches_espn_names(self):
        n.subscribe_team("thorns-fan", "", team_id="portland-thorns")
        n.subscribe_team("gotham-fan", "NJ/NY Gotham FC", NWSL)
        subs = set(n.for_game_or_team_subscribers(
            "w-3", NWSL, home_team="Portland Thorns FC", away_team="Gotham FC"))
        self.assertEqual(subs, {"thorns-fan", "gotham-fan"})
        # Men's Portland (MLS) game does not reach the Thorns fan.
        self.assertEqual(
            n.for_game_or_team_subscribers("m-2", "United States/MLS", home_team="Portland Timbers",
                                           away_team="Seattle Sounders"),
            [],
        )

    def test_womens_follow_survives_reload(self):
        n.subscribe_team("womens-fan", "Arsenal", WSL)
        self._reset()
        n.init_subscriptions_from_sqlite()
        self.assertEqual(n.for_team_tokens("Arsenal Women", competition=WSL), ["womens-fan"])
        self.assertEqual(n.for_team_tokens("Arsenal", competition=PL), [])

    def test_push_to_start_for_womens_game_skips_mens_followers(self):
        ko = datetime.now(timezone.utc) + timedelta(minutes=4)
        n.subscribe_team("mens-fan", "Arsenal", PL)
        n.subscribe_team("womens-fan", "Arsenal Women", WSL)
        n.register_push_to_start("pts-mens", "mens-fan")
        n.register_push_to_start("pts-womens", "womens-fan")
        with lp._live_scores_lock:
            lp._live_scores[WSL] = {"competition": WSL, "games": [{
                "match_id": "w-10", "home_team": "Arsenal", "away_team": "Chelsea",
                "sync_key": "s-w-10", "status": "pre", "kickoff_utc": _iso(ko),
                "home_score": None, "away_score": None, "period": "Scheduled", "clock": "",
            }]}
        lp._dispatch_live_activity_starts()
        starts = [e["token"] for e in n._apns_notification_queue if e.get("event") == "start"]
        self.assertEqual(starts, ["pts-womens"])


class PushToStartOwnershipTests(_Base):
    def _pre_game(self):
        ko = datetime.now(timezone.utc) + timedelta(minutes=4)
        with lp._live_scores_lock:
            lp._live_scores[PL] = {"competition": PL, "games": [{
                "match_id": "m-20", "home_team": "Chelsea", "away_team": "Arsenal",
                "sync_key": "s-m-20", "status": "pre", "kickoff_utc": _iso(ko),
                "home_score": None, "away_score": None, "period": "Scheduled", "clock": "",
            }]}

    def _starts(self):
        return [e["token"] for e in n._apns_notification_queue if e.get("event") == "start"]

    def test_rotated_device_token_takes_over_push_to_start_token(self):
        # Same phone, device token rotated: both device rows follow Chelsea.
        for dev in ("old-dev", "new-dev"):
            n.subscribe_team(dev, "Chelsea", PL)
            n.register_push_to_start("pts-phone", dev)
        self.assertEqual(n.push_to_start_token_for_device("old-dev"), "")
        self.assertEqual(n.push_to_start_token_for_device("new-dev"), "pts-phone")
        rows = [r for r in sqlite_store.load_all_notification_subs() if r["sub_type"] == "la_push_to_start"]
        self.assertEqual([(r["token"], r["sub_target"]) for r in rows], [("pts-phone", "new-dev")])

        self._pre_game()
        lp._dispatch_live_activity_starts()
        self.assertEqual(self._starts(), ["pts-phone"])  # exactly one Live Activity

    def test_reload_collapses_duplicate_owners_to_latest(self):
        now = datetime.now(timezone.utc)
        for dev, age_min in (("old-dev", 60), ("mid-dev", 30), ("new-dev", 1)):
            sqlite_store.save_notification_sub("pts-phone", "la_push_to_start", dev, device_token=dev)
            n.subscribe_team(dev, "Chelsea", PL)
        # Stamp created_at so the newest owner is unambiguous.
        conn = sqlite_store._open_ready(self.db_path)
        try:
            for dev, age_min in (("old-dev", 60), ("mid-dev", 30), ("new-dev", 1)):
                conn.execute(
                    "UPDATE notification_subscriptions SET created_at=? WHERE sub_type='la_push_to_start' AND sub_target=?",
                    (_iso(now - timedelta(minutes=age_min)), dev),
                )
            conn.commit()
        finally:
            conn.close()
        self._reset()
        n.init_subscriptions_from_sqlite()
        self.assertEqual(n.push_to_start_token_for_device("new-dev"), "pts-phone")
        self.assertEqual(n.push_to_start_token_for_device("old-dev"), "")
        self.assertEqual(n.push_to_start_token_for_device("mid-dev"), "")
        rows = [r["sub_target"] for r in sqlite_store.load_all_notification_subs() if r["sub_type"] == "la_push_to_start"]
        self.assertEqual(rows, ["new-dev"])

        self._pre_game()
        lp._dispatch_live_activity_starts()
        self.assertEqual(self._starts(), ["pts-phone"])

    def test_dispatch_dedupes_shared_token_even_if_registry_is_inconsistent(self):
        for dev in ("dev-1", "dev-2"):
            n.subscribe_team(dev, "Chelsea", PL)
        # Force an inconsistent registry (e.g. written by an older backend).
        n._push_to_start_tokens["dev-1"] = {"token": "pts-shared", "device_token": "dev-1"}
        n._push_to_start_tokens["dev-2"] = {"token": "pts-shared", "device_token": "dev-2"}
        self._pre_game()
        lp._dispatch_live_activity_starts()
        self.assertEqual(self._starts(), ["pts-shared"])


class _FakeResp:
    def __init__(self, status: int, reason: str | None = None):
        self.status_code = status
        self.is_success = 200 <= status < 300
        self.headers = {"apns-id": "x"}
        self.text = "" if reason is None else '{"reason": "%s"}' % reason

    def json(self):
        import json
        return json.loads(self.text) if self.text else {}


class _FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, json=None, headers=None):
        self.calls.append(url)
        return self.responses.pop(0)


class TokenPruneTests(_Base):
    LONG_TOKEN = "80" + "ab" * 79  # 160 hex chars, like current sandbox device tokens

    def _send(self, responses, sandbox=None):
        client = _FakeClient(responses)
        with mock.patch.object(n, "_generate_apns_jwt", return_value="jwt"), \
                mock.patch.object(n, "_get_apns_client", return_value=client), \
                mock.patch.object(n, "prune_dead_token") as prune:
            res = n.apns_send_detailed(self.LONG_TOKEN, {"aps": {}}, sandbox=sandbox)
        return res, prune, client

    def test_long_token_rejected_by_both_environments_is_pruned(self):
        res, prune, client = self._send([_FakeResp(400, "BadDeviceToken"), _FakeResp(400, "BadDeviceToken")])
        self.assertFalse(res["ok"])
        self.assertEqual(len(client.calls), 2)
        prune.assert_called_once()

    def test_forced_environment_rejection_never_prunes(self):
        res, prune, client = self._send([_FakeResp(400, "BadDeviceToken")], sandbox=True)
        self.assertEqual(len(client.calls), 1)
        prune.assert_not_called()

    def test_other_environment_failing_differently_does_not_prune(self):
        _, prune, _ = self._send([_FakeResp(400, "BadDeviceToken"), _FakeResp(500, "InternalServerError")])
        prune.assert_not_called()

    def test_other_environment_accepting_does_not_prune(self):
        res, prune, _ = self._send([_FakeResp(400, "BadDeviceToken"), _FakeResp(200)])
        self.assertTrue(res["ok"])
        prune.assert_not_called()

    def test_unregistered_410_is_pruned(self):
        _, prune, _ = self._send([_FakeResp(410, "Unregistered")])
        prune.assert_called_once()


if __name__ == "__main__":
    unittest.main()
