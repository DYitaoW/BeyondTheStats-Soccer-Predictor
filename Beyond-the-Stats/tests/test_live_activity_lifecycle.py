"""Regression tests for the ActivityKit start/update/end lifecycle and APNs audit fixes.

Covers:
- Push-to-start ("event": "start") sent 5 minutes before kickoff to followers
  with a registered push-to-start token (once, never before the window, never
  to devices that already run an activity for the match).
- aps.event is always start/update/end; UI labels ride in content-state.event.
- Full final content-state on "end"; STATUS_END_REGULAR_TIME is not full time.
- Period-change (2nd half) and score-correction updates; no duplicate score push.
- Devices with an active Live Activity get no duplicate regular push.
- Admin auth on broadcast / LA update / LA end / LA listing.
- Device unregister cleanup, unsubscribe without competition, team alias reload,
  LA tokens not leaking into the device registry, LA rows deleted at match end,
  token environment persistence, and no-store caching on notification routes.

All kickoff times are relative to wall-clock time (no hardcoded dates).
"""
from __future__ import annotations

import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
WEBSITE_DIR = ROOT / "Website"
for p in (ROOT, WEBSITE_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from fastapi.testclient import TestClient

from Website.app import app
import config
import notifications as n
import live_poller as lp
from shared import sqlite_store

COMP = "England/Premier League"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Base(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "lifecycle.db"
        sqlite_store.ensure_store(self.db_path)
        self._orig_default_db = sqlite_store._default_db_path
        sqlite_store._default_db_path = lambda: self.db_path
        self._orig_store = n.sqlite_store
        n.sqlite_store = sqlite_store
        self._reset_memory()

    def tearDown(self):
        self._reset_memory()
        n.sqlite_store = self._orig_store
        sqlite_store._default_db_path = self._orig_default_db
        self.temp_dir.cleanup()

    def _reset_memory(self):
        with lp._live_scores_lock:
            lp._live_scores.clear()
        with lp._notified_events_lock:
            lp._notified_events.clear()
        with lp._match_tracker_lock:
            lp._match_tracker.clear()
        n._apns_notification_queue.clear()
        n._live_activities.clear()
        n._match_notification_subscriptions.clear()
        n._match_subscription_events.clear()
        n._team_notification_subscriptions.clear()
        n._team_subscription_events.clear()
        n._push_to_start_tokens.clear()
        n._push_started.clear()
        n._token_environment_cache.clear()
        n.ios_device_tokens.clear()
        n.device_tokens.clear()
        n._device_la_settings.clear()
        n._la_channels.clear()
        n._channel_activity_devices.clear()
        lp._la_timer_anchors.clear()

    def _set_game(self, **game):
        base = {
            "match_id": "m-500",
            "home_team": "Arsenal",
            "away_team": "Chelsea",
            "sync_key": "s-arsenal-chelsea",
        }
        base.update(game)
        with lp._live_scores_lock:
            lp._live_scores[COMP] = {"competition": COMP, "games": [base]}
        return base

    def _la(self):
        return [e for e in n._apns_notification_queue if e.get("type") == "liveactivity"]

    def _regular(self):
        return [e for e in n._apns_notification_queue if e.get("type") != "liveactivity"]


class PushToStartTests(_Base):
    def _pre_game(self, minutes_to_kickoff: float):
        ko = datetime.now(timezone.utc) + timedelta(minutes=minutes_to_kickoff)
        return self._set_game(status="pre", home_score=None, away_score=None,
                              kickoff_utc=_iso(ko), period="Scheduled")

    def test_start_sent_five_minutes_before_kickoff_once(self):
        n.subscribe_team("dev-a", "Arsenal", COMP)
        n.register_push_to_start("pts-a", "dev-a", environment="sandbox")

        # 20 minutes out: outside the 5-minute window → nothing.
        self._pre_game(20)
        self.assertEqual(lp._dispatch_live_activity_starts(), 0)
        self.assertEqual(len(n._apns_notification_queue), 0)

        # 4 minutes out: inside the window → exactly one start to the pts token.
        self._pre_game(4)
        self.assertEqual(lp._dispatch_live_activity_starts(), 1)
        la = self._la()
        self.assertEqual(len(la), 1)
        self.assertEqual(la[0]["token"], "pts-a")
        self.assertEqual(la[0]["event"], "start")

        payload, priority = n.build_live_activity_aps(la[0])
        aps = payload["aps"]
        self.assertEqual(aps["event"], "start")
        self.assertEqual(priority, 10)
        self.assertEqual(aps["attributes-type"], config.APNS_LA_ATTRIBUTES_TYPE)
        self.assertEqual(aps["attributes"]["home_team"], "Arsenal")
        self.assertEqual(aps["attributes"]["match_id"], "m-500")
        self.assertIn("alert", aps)
        self.assertEqual(aps["alert"]["sound"], "default")
        self.assertIn("Kickoff in", aps["alert"]["title"])
        self.assertIn("stale-date", aps)
        self.assertEqual(aps["content-state"]["event"], "prematch")
        self.assertIsNone(aps["content-state"]["home_score"])

        # Next scheduler tick: never a duplicate start.
        n._apns_notification_queue.clear()
        self.assertEqual(lp._dispatch_live_activity_starts(), 0)

    def test_start_dedupe_survives_restart(self):
        n.subscribe_team("dev-b", "Arsenal", COMP)
        n.register_push_to_start("pts-b", "dev-b")
        self._pre_game(3)
        self.assertEqual(lp._dispatch_live_activity_starts(), 1)

        # Simulated restart: memory wiped, state reloaded from SQLite.
        self._reset_memory()
        n.init_subscriptions_from_sqlite()
        self.assertEqual(n.push_to_start_token_for_device("dev-b"), "pts-b")
        self._pre_game(2)
        self.assertEqual(lp._dispatch_live_activity_starts(), 0)

    def test_no_start_for_device_already_running_activity(self):
        n.subscribe_match("dev-c", "m-500", COMP)
        n.register_push_to_start("pts-c", "dev-c")
        n.register("act-c", "dev-c", "m-500", COMP)
        self._pre_game(2)
        self.assertEqual(lp._dispatch_live_activity_starts(), 0)
        self.assertEqual(len(n._apns_notification_queue), 0)

    def test_no_start_without_subscription_or_pts_token(self):
        n.register_push_to_start("pts-d", "dev-d")  # follows nothing
        n.subscribe_team("dev-e", "Arsenal", COMP)  # follows, but no pts token
        self._pre_game(2)
        self.assertEqual(lp._dispatch_live_activity_starts(), 0)

    def test_pts_token_rejected_by_apns_is_pruned(self):
        n.register_push_to_start("pts-f", "dev-f")
        n.prune_dead_token("pts-f", is_live_activity=True)
        self.assertEqual(n.push_to_start_token_for_device("dev-f"), "")


class DispatchLifecycleTests(_Base):
    def _live(self, **kw):
        defaults = dict(status="in", home_score=0, away_score=0, clock="30'",
                        period="1st Half", status_type="STATUS_FIRST_HALF")
        defaults.update(kw)
        return self._set_game(**defaults)

    def test_aps_event_always_valid_for_in_match_updates(self):
        n.register("act-1", "", "m-500", COMP)
        n.send_live_activity_update("m-500", COMP, {"home_score": 1}, event="red card",
                                    alert={"title": "Red Card", "body": "x"})
        payload, priority = n.build_live_activity_aps(self._la()[0])
        self.assertEqual(payload["aps"]["event"], "update")
        self.assertEqual(payload["aps"]["content-state"]["event"], "red card")
        self.assertEqual(priority, 10)

    def test_device_with_live_activity_gets_no_duplicate_regular_push(self):
        n.subscribe_match("dev-la", "m-500", COMP)
        n.subscribe_match("dev-plain", "m-500", COMP)
        n.register("act-la", "dev-la", "m-500", COMP)
        self._live()
        lp._dispatch_match_event_notifications()
        n._apns_notification_queue.clear()

        self._live(home_score=1, clock="31'",
                   goalscorers=[{"team": "home", "scorer": "Saka", "minute": "31'", "type": "Goal"}])
        lp._dispatch_match_event_notifications()
        regular_tokens = {e["token"] for e in self._regular()}
        self.assertEqual(regular_tokens, {"dev-plain"})
        la = self._la()
        self.assertEqual(len(la), 1)  # exactly one LA push per goal (no duplicate score push)
        self.assertEqual(la[0]["content_state"]["event"], "goal")
        self.assertEqual(self._regular()[0]["collapse_id"], "goal|m-500|1-0")

    def test_end_carries_full_content_state(self):
        n.register("act-ft", "", "m-500", COMP)
        self._live(home_score=2, away_score=1, clock="90'", period="2nd Half",
                   status_type="STATUS_SECOND_HALF")
        lp._dispatch_match_event_notifications()
        n._apns_notification_queue.clear()

        self._live(status="post", home_score=2, away_score=1, clock="FT", period="FT",
                   status_type="STATUS_FULL_TIME")
        lp._dispatch_match_event_notifications()
        la = self._la()
        self.assertEqual(len(la), 1)
        self.assertEqual(la[0]["event"], "end")
        state = la[0]["content_state"]
        for key in ("match_id", "home_team", "away_team", "home_score", "away_score",
                    "status", "competition", "half_start_time", "kickoff_date", "prob_home"):
            self.assertIn(key, state)
        self.assertEqual(state["status"], "FT")
        self.assertEqual(state["home_team"], "Arsenal")
        self.assertEqual((state["home_score"], state["away_score"]), (2, 1))

    def test_end_of_regular_time_is_not_full_time(self):
        n.register("act-et", "", "m-500", COMP)
        self._live(home_score=1, away_score=1, clock="90'", period="2nd Half",
                   status_type="STATUS_SECOND_HALF")
        lp._dispatch_match_event_notifications()
        n._apns_notification_queue.clear()

        self._live(home_score=1, away_score=1, clock="90'", period="FT",
                   status_type="STATUS_END_REGULAR_TIME")
        lp._dispatch_match_event_notifications()
        self.assertFalse(any(e.get("event") == "end" for e in self._la()))
        self.assertEqual(len(n.for_match("m-500", COMP)), 1)  # activity still registered
        labels = [e["content_state"].get("event") for e in self._la()]
        self.assertIn("end of regular time", labels)

    def test_second_half_kickoff_refreshes_activity(self):
        n.register("act-2h", "", "m-500", COMP)
        self._live(clock="HT", period="Halftime", status_type="STATUS_HALFTIME")
        lp._dispatch_match_event_notifications()
        n._apns_notification_queue.clear()

        self._live(clock="46'", period="2nd Half", status_type="STATUS_SECOND_HALF")
        lp._dispatch_match_event_notifications()
        la = self._la()
        self.assertEqual(len(la), 1)
        self.assertEqual(la[0]["content_state"]["event"], "second half")
        payload, priority = n.build_live_activity_aps(la[0])
        self.assertEqual(payload["aps"]["event"], "update")
        # Halftime ending is a first-class alert at priority 10.
        self.assertEqual(priority, 10)
        self.assertEqual(payload["aps"]["alert"]["title"], "Second Half")
        # Clock resumes: running, anchored ~45:00+ in the past.
        state = payload["aps"]["content-state"]
        self.assertTrue(state["timer_running"])
        self.assertIsNone(state["timer_paused_at"])

        # Same period next cycle: no repeat.
        n._apns_notification_queue.clear()
        lp._dispatch_match_event_notifications()
        self.assertEqual(len(self._la()), 0)

    def test_var_reversal_pushes_score_correction(self):
        n.register("act-var", "", "m-500", COMP)
        self._live(home_score=1, clock="40'")
        lp._dispatch_match_event_notifications()
        n._apns_notification_queue.clear()

        self._live(home_score=0, clock="42'")
        lp._dispatch_match_event_notifications()
        la = self._la()
        self.assertEqual(len(la), 1)
        self.assertEqual(la[0]["content_state"]["event"], "score")
        self.assertEqual(la[0]["content_state"]["home_score"], 0)
        self.assertEqual(la[0]["priority"], 10)

    def test_finished_on_first_observation_ends_orphan_activity(self):
        n.register("act-orphan", "", "m-500", COMP)
        self._live(status="post", home_score=1, away_score=0, clock="FT", period="FT",
                   status_type="STATUS_FULL_TIME")
        lp._dispatch_match_event_notifications()
        la = self._la()
        self.assertEqual(len(la), 1)
        self.assertEqual(la[0]["event"], "end")
        self.assertIsNone(la[0]["alert"])  # silent cleanup, no stale banner
        self.assertEqual(len(self._regular()), 0)
        self.assertEqual(len(n.for_match("m-500", COMP)), 0)


class PersistenceAndCleanupTests(_Base):
    def test_live_activity_rows_deleted_at_match_end(self):
        n.register("act-x", "dev-x", "m-600", COMP, sync_key="2026|a|b")
        rows = [r for r in sqlite_store.load_all_notification_subs() if r["sub_type"] == "live_activity"]
        self.assertEqual(len(rows), 2)  # match_id + sync_key rows
        self.assertEqual(rows[0]["device_token"], "dev-x")
        n.send_live_activity_end("m-600", COMP, {"status": "FT"}, sync_key="2026|a|b")
        rows = [r for r in sqlite_store.load_all_notification_subs() if r["sub_type"] == "live_activity"]
        self.assertEqual(rows, [])

    def test_live_activity_tokens_not_loaded_as_device_tokens(self):
        n.register("act-y", "dev-y", "m-601", COMP)
        n.register_push_to_start("pts-y", "dev-y")
        self._reset_memory()
        n.init_subscriptions_from_sqlite()
        self.assertNotIn("act-y", n.ios_device_tokens)
        self.assertNotIn("pts-y", n.ios_device_tokens)
        # device_token metadata survives restart (needed for duplicate-banner dedupe).
        self.assertEqual(n.live_activity_device_tokens("m-601", COMP), {"dev-y"})

    def test_team_alias_subscription_survives_restart(self):
        n.subscribe_team("dev-t", "Manchester United", COMP)
        self._reset_memory()
        n.init_subscriptions_from_sqlite()
        # Poller uses the canonical name.
        self.assertIn("dev-t", n.for_team_tokens("Man United", competition=COMP))
        # Unsubscribe removes every registry key.
        n.unsubscribe_team("dev-t", "Manchester United", competition=COMP)
        self.assertNotIn("dev-t", n.for_team_tokens("Man United", competition=COMP))

    def test_unsubscribe_match_without_competition(self):
        n.subscribe_match("dev-u", "m-700", COMP)
        self.assertTrue(n.unsubscribe_match("dev-u", "m-700"))
        self.assertEqual(n.for_match_tokens("m-700", COMP), [])
        rows = [r for r in sqlite_store.load_all_notification_subs() if r["sub_type"] == "match"]
        self.assertEqual(rows, [])

    def test_token_environment_persisted(self):
        n.subscribe_match("dev-env", "m-800", COMP, environment="sandbox")
        self._reset_memory()
        n.init_subscriptions_from_sqlite()
        self.assertIs(n.get_token_environment("dev-env"), True)

    def test_alert_payload_has_deep_link_metadata(self):
        payload = n.build_alert_payload({
            "title": "Goal", "body": "x", "match_id": "m-1", "sync_key": "s-1",
            "competition": COMP, "event": "goal",
        })
        self.assertEqual(payload["aps"]["thread-id"], "match-m-1")
        self.assertEqual(payload["sync_key"], "s-1")
        self.assertEqual(payload["event"], "goal")


class EndpointTests(_Base):
    def setUp(self):
        super().setUp()
        self._orig_token = config.MUTATION_API_TOKEN
        config.MUTATION_API_TOKEN = "lifecycle-admin"
        self.admin = {"X-Admin-Token": "lifecycle-admin"}
        self.client = TestClient(app, raise_server_exceptions=False)

    def tearDown(self):
        config.MUTATION_API_TOKEN = self._orig_token
        super().tearDown()

    def test_mutation_endpoints_require_admin(self):
        for path, body in (
            ("/api/notifications", {"title": "t", "body": "b", "audience": "all"}),
            ("/api/live-activities/update", {"match_id": "1", "competition": COMP, "content_state": {}}),
            ("/api/live-activities/end", {"match_id": "1", "competition": COMP}),
        ):
            self.assertEqual(self.client.post(path, json=body).status_code, 401, path)
            self.assertEqual(self.client.post(path, json=body, headers=self.admin).status_code, 200, path)
        self.assertEqual(self.client.get("/api/live-activities").status_code, 401)
        self.assertEqual(self.client.get("/api/live-activities", headers=self.admin).status_code, 200)

    def test_unregister_device_removes_everything(self):
        self.client.post("/api/notifications/register", json={
            "token": "dev-z", "favorite_teams": ["Arsenal"], "push_to_start_token": "pts-z",
        })
        self.assertEqual(n.push_to_start_token_for_device("dev-z"), "pts-z")
        self.client.post("/api/notifications/subscribe", json={"token": "dev-z", "match_id": "m-900"})
        # No platform field: must still remove the iOS token.
        res = self.client.post("/api/notifications/unregister", json={"token": "dev-z"})
        self.assertTrue(res.json()["removed"])
        self.assertNotIn("dev-z", n.ios_device_tokens)
        self.assertEqual(n.for_team_tokens("Arsenal", competition=COMP), [])
        self.assertEqual(n.for_match_tokens("m-900", ""), [])
        self.assertEqual(n.push_to_start_token_for_device("dev-z"), "")
        self.assertEqual(sqlite_store.load_all_notification_subs(), [])

    def test_push_to_start_endpoints(self):
        res = self.client.post("/api/live-activities/push-to-start/register", json={
            "push_to_start_token": "pts-q", "device_token": "dev-q", "environment": "production",
        })
        self.assertTrue(res.json()["registered"])
        self.assertEqual(n.push_to_start_token_for_device("dev-q"), "pts-q")
        res = self.client.post("/api/live-activities/push-to-start/unregister", json={"device_token": "dev-q"})
        self.assertTrue(res.json()["removed"])
        self.assertEqual(n.push_to_start_token_for_device("dev-q"), "")

    def test_notification_routes_are_not_cached(self):
        res = self.client.get("/api/notifications/preferences?token=dev-cache")
        self.assertEqual(res.headers.get("cache-control"), "no-store")
        res = self.client.post("/api/notifications/subscribe", json={"token": "dev-cache", "team": "Arsenal"})
        self.assertEqual(res.headers.get("cache-control"), "no-store")


if __name__ == "__main__":
    unittest.main()
