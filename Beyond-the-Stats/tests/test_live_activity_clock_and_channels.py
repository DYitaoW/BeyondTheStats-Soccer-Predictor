"""Regression tests: Live Activity clock, period events, red-card dedupe,
frequent-updates settings, iOS 18 broadcast channels, all-or-none
notifications, and the /api/startup upcoming list.

All times are relative to wall-clock time (no hardcoded dates).
"""
from __future__ import annotations

import sys
import time
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

from fastapi.testclient import TestClient

from Website.app import app
import config
import notifications as n
import live_poller as lp
from shared import sqlite_store

COMP = "England/Premier League"
SWIFT_EPOCH = 978307200.0


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Base(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "clock.db"
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
        with lp._notified_events_lock:
            lp._notified_events.clear()
        with lp._match_tracker_lock:
            lp._match_tracker.clear()
        lp._la_timer_anchors.clear()
        for store in (
            n._apns_notification_queue, n._live_activities, n._match_notification_subscriptions,
            n._match_subscription_events, n._team_notification_subscriptions, n._team_subscription_events,
            n._push_to_start_tokens, n._push_started, n._token_environment_cache, n.ios_device_tokens,
            n.device_tokens, n._device_la_settings, n._la_channels, n._channel_activity_devices,
        ):
            store.clear()

    def _set_game(self, polled_at: datetime | None = None, **game):
        base = {
            "match_id": "m-900", "home_team": "Arsenal", "away_team": "Chelsea",
            "home_team_id": "359", "away_team_id": "363", "sync_key": "s-ars-che",
            "status": "in", "home_score": 0, "away_score": 0, "clock": "1'",
            "period": "1st Half", "status_type": "STATUS_FIRST_HALF",
        }
        base.update(game)
        with lp._live_scores_lock:
            lp._live_scores[COMP] = {
                "competition": COMP,
                "last_polled_utc": (polled_at or datetime.now(timezone.utc)).isoformat(),
                "games": [base],
            }
        return base

    def _la(self):
        return [e for e in n._apns_notification_queue if e.get("type") == "liveactivity"]

    def _regular(self):
        return [e for e in n._apns_notification_queue if e.get("type") not in ("liveactivity", "broadcast")]

    def _broadcasts(self):
        return [e for e in n._apns_notification_queue if e.get("type") == "broadcast"]


class ClockTests(_Base):
    def test_swift_reference_date_conversion(self):
        self.assertEqual(lp._swift_date(SWIFT_EPOCH), 0.0)
        self.assertIsNone(lp._swift_date(None))

    def test_running_clock_anchor_is_precise_and_stable(self):
        now = time.time()
        g = self._set_game(clock="23'", clock_seconds=1350.0)
        f1 = lp._la_timer_fields(g, polled_at=now)
        self.assertTrue(f1["timer_running"])
        self.assertIsNone(f1["timer_paused_at"])
        # now - timer_start == elapsed match time
        self.assertAlmostEqual((now - (f1["timer_start"] + SWIFT_EPOCH)), 1350.0, delta=0.01)

        # One minute later ESPN reports 24' with ~1405s — anchor must not jitter.
        g2 = self._set_game(clock="24'", clock_seconds=1405.0)
        f2 = lp._la_timer_fields(g2, polled_at=now + 60)
        self.assertEqual(f1["timer_start"], f2["timer_start"])

    def test_halftime_pauses_at_45_and_second_half_resumes(self):
        now = time.time()
        g = self._set_game(clock="44'", clock_seconds=2640.0)
        running = lp._la_timer_fields(g, polled_at=now)

        ht = self._set_game(clock="HT", period="Halftime", status_type="STATUS_HALFTIME", clock_seconds=2700.0)
        paused = lp._la_timer_fields(ht, polled_at=now + 300)
        self.assertFalse(paused["timer_running"])
        self.assertEqual(paused["timer_start"], running["timer_start"])
        self.assertAlmostEqual(paused["timer_paused_at"] - paused["timer_start"], 45 * 60, delta=0.01)

        h2 = self._set_game(clock="46'", period="2nd Half", status_type="STATUS_SECOND_HALF", clock_seconds=2710.0)
        resumed = lp._la_timer_fields(h2, polled_at=now + 1200)
        self.assertTrue(resumed["timer_running"])
        self.assertAlmostEqual((now + 1200) - (resumed["timer_start"] + SWIFT_EPOCH), 2710.0, delta=0.01)

    def test_minute_display_fallback_without_clock_seconds(self):
        now = time.time()
        g = self._set_game(clock="10'", clock_seconds=None)
        f = lp._la_timer_fields(g, polled_at=now)
        self.assertAlmostEqual(now - (f["timer_start"] + SWIFT_EPOCH), 570.0, delta=0.01)

    def test_pre_match_has_kickoff_time_only(self):
        ko = datetime.now(timezone.utc) + timedelta(minutes=30)
        g = self._set_game(status="pre", kickoff_utc=_iso(ko), clock="", period="Scheduled")
        f = lp._la_timer_fields(g)
        self.assertFalse(f["timer_running"])
        self.assertIsNone(f["timer_start"])
        self.assertAlmostEqual(f["kickoff_time"] + SWIFT_EPOCH, ko.timestamp(), delta=1.0)

    def test_content_state_carries_timer_fields(self):
        g = self._set_game(clock="30'", clock_seconds=1790.0)
        state = lp._la_content_state(g, COMP)
        for key in ("timer_start", "timer_paused_at", "timer_running", "kickoff_time", "half_start_time"):
            self.assertIn(key, state)


class PeriodEventTests(_Base):
    def _prime(self, **kw):
        self._set_game(**kw)
        lp._dispatch_match_event_notifications()
        n._apns_notification_queue.clear()

    def test_halftime_start_and_end_both_alert(self):
        n.subscribe_match("dev-plain", "m-900", COMP)
        n.register("act-1", "dev-la", "m-900", COMP)
        self._prime(clock="45'", clock_seconds=2700.0)

        # Halftime begins: LA update (paused clock) + regular push.
        self._set_game(clock="HT", period="Halftime", status_type="STATUS_HALFTIME")
        lp._dispatch_match_event_notifications()
        la = self._la()
        self.assertEqual(la[0]["content_state"]["event"], "halftime")
        self.assertFalse(la[0]["content_state"]["timer_running"])
        self.assertEqual([e["event"] for e in self._regular()], ["ht"])
        n._apns_notification_queue.clear()

        # Halftime ends: second-half alert, regular push, running clock.
        self._set_game(clock="46'", period="2nd Half", status_type="STATUS_SECOND_HALF", clock_seconds=2705.0)
        lp._dispatch_match_event_notifications()
        la = self._la()
        self.assertEqual(len(la), 1)
        self.assertEqual(la[0]["content_state"]["event"], "second half")
        self.assertEqual(la[0]["alert"]["title"], "Second Half")
        self.assertTrue(la[0]["content_state"]["timer_running"])
        regular = self._regular()
        self.assertEqual([(e["token"], e["event"]) for e in regular], [("dev-plain", "second_half")])

    def test_extra_time_and_penalties(self):
        n.register("act-et", "", "m-900", COMP)
        n.subscribe_match("dev-et", "m-900", COMP)
        self._prime(clock="90'", period="2nd Half", status_type="STATUS_SECOND_HALF", home_score=1, away_score=1)

        self._set_game(clock="90'", period="FT", status_type="STATUS_END_REGULAR_TIME", home_score=1, away_score=1)
        lp._dispatch_match_event_notifications()
        la = self._la()
        self.assertEqual(la[0]["content_state"]["event"], "end of regular time")
        state = la[0]["content_state"]
        self.assertFalse(state["timer_running"])
        self.assertAlmostEqual(state["timer_paused_at"] - state["timer_start"], 90 * 60, delta=0.01)
        n._apns_notification_queue.clear()

        self._set_game(clock="91'", period="ET 1st Half", status_type="STATUS_EXTRA_FIRST_HALF",
                       home_score=1, away_score=1, clock_seconds=5410.0)
        lp._dispatch_match_event_notifications()
        self.assertEqual(self._la()[0]["alert"]["title"], "Extra Time")
        self.assertIn(("dev-et", "extra_time"), [(e["token"], e["event"]) for e in self._regular()])
        n._apns_notification_queue.clear()

        self._set_game(clock="120'", period="Penalties", status_type="STATUS_SHOOTOUT", home_score=1, away_score=1)
        lp._dispatch_match_event_notifications()
        la = self._la()
        self.assertEqual(la[0]["alert"]["title"], "Penalties")
        self.assertFalse(la[0]["content_state"]["timer_running"])


class RedCardDedupeTests(_Base):
    def _prime(self):
        n.subscribe_match("dev-rc", "m-900", COMP)
        self._set_game(clock="60'")
        lp._dispatch_match_event_notifications()
        n._apns_notification_queue.clear()

    def _red_alerts(self):
        return [e for e in self._regular() if e["event"] == "red_card"]

    def test_same_card_from_both_feeds_alerts_once(self):
        self._prime()
        self._set_game(
            clock="68'",
            red_cards=[{"team": "home", "player": "Bukayo Saka", "minute": "67'"}],
            key_events=[{"type": "Red Card", "athlete_name": "B. Saka", "clock": "67", "team_id": "359"}],
        )
        lp._dispatch_match_event_notifications()
        self.assertEqual(len(self._red_alerts()), 1)
        self.assertIn("Bukayo Saka (Arsenal)", self._red_alerts()[0]["body"])

        # Later cycle: key event without athlete name for the same minute → still one.
        n._apns_notification_queue.clear()
        self._set_game(
            clock="70'",
            red_cards=[{"team": "home", "player": "Bukayo Saka", "minute": "67'"}],
            key_events=[{"type": "Red Card", "athlete_name": "", "text": "Saka sent off", "clock": "67'"}],
        )
        lp._dispatch_match_event_notifications()
        self.assertEqual(self._red_alerts(), [])

    def test_accented_names_and_suffixes_match(self):
        self._prime()
        self._set_game(
            clock="80'",
            red_cards=[{"team": "away", "player": "Vinícius Júnior", "minute": "79'"}],
            key_events=[{"type": "Red Card", "athlete_name": "Vinicius Jr.", "clock": "79'", "team_id": "363"}],
        )
        lp._dispatch_match_event_notifications()
        self.assertEqual(len(self._red_alerts()), 1)

    def test_distinct_cards_both_alert(self):
        self._prime()
        self._set_game(
            clock="75'",
            red_cards=[
                {"team": "home", "player": "Declan Rice", "minute": "74'"},
                {"team": "away", "player": "Cole Palmer", "minute": "74'"},
            ],
        )
        lp._dispatch_match_event_notifications()
        self.assertEqual(len(self._red_alerts()), 2)

    def test_restart_mid_match_does_not_realert_existing_cards(self):
        n.subscribe_match("dev-rc", "m-900", COMP)
        game = dict(clock="70'", red_cards=[{"team": "home", "player": "Declan Rice", "minute": "30'"}])
        self._set_game(**game)
        lp._dispatch_match_event_notifications()  # first observation (fresh process)
        self._set_game(**{**game, "clock": "71'"})
        lp._dispatch_match_event_notifications()
        self.assertEqual(self._red_alerts(), [])


class SettingsTests(_Base):
    def test_frequent_updates_disabled_downgrades_silent_updates(self):
        n.register("act-fast", "dev-fast", "m-900", COMP)
        n.register("act-slow", "dev-slow", "m-900", COMP)
        n.set_device_la_settings("dev-slow", frequent_updates=False)
        n.send_live_activity_update("m-900", COMP, {"home_score": 0}, event="score", priority=10)
        prio = {e["token"]: n.build_live_activity_aps(e)[1] for e in self._la()}
        self.assertEqual(prio, {"act-fast": 10, "act-slow": 5})

        # Alerts are never downgraded.
        n._apns_notification_queue.clear()
        n.send_live_activity_update("m-900", COMP, {}, event="goal", alert={"title": "Goal", "body": "x"})
        self.assertEqual({n.build_live_activity_aps(e)[1] for e in self._la()}, {10})

    def test_activities_disabled_blocks_push_to_start(self):
        ko = datetime.now(timezone.utc) + timedelta(minutes=3)
        n.subscribe_team("dev-off", "Arsenal", COMP)
        n.register_push_to_start("pts-off", "dev-off")
        n.set_device_la_settings("dev-off", activities_enabled=False)
        self._set_game(status="pre", kickoff_utc=_iso(ko), clock="", period="Scheduled",
                       home_score=None, away_score=None)
        self.assertEqual(lp._dispatch_live_activity_starts(), 0)

    def test_settings_persist_and_ios_version_implies_broadcast(self):
        n.set_device_la_settings("dev-18", ios_version="18.2", frequent_updates="false")
        self._reset()
        n.init_subscriptions_from_sqlite()
        s = n.get_device_la_settings("dev-18")
        self.assertTrue(s["supports_broadcast"])
        self.assertFalse(s["frequent_updates"])
        self.assertTrue(s["activities_enabled"])  # default


class BroadcastChannelTests(_Base):
    def setUp(self):
        super().setUp()
        config.APNS_BROADCAST_ENABLED = True
        self._orig_bundle = config.APNS_BUNDLE_ID
        config.APNS_BUNDLE_ID = "com.example.bts"

    def tearDown(self):
        config.APNS_BUNDLE_ID = self._orig_bundle
        super().tearDown()

    def _fake_channel_api(self):
        calls = []

        def _fake(method, environment, channel_id="", body=None):
            calls.append((method, environment, channel_id, body))
            if method == "POST":
                return {"ok": True, "status_code": 201, "channel_id": f"chan-{len(calls)}=="}
            return {"ok": True, "status_code": 204, "channel_id": channel_id}

        return calls, mock.patch.object(n, "_apns_channel_request", side_effect=_fake)

    def test_channel_created_once_per_match_and_environment(self):
        calls, patcher = self._fake_channel_api()
        with patcher:
            a = n.get_or_create_match_channel("m-900", "s-ars-che", COMP, environment="production")
            b = n.get_or_create_match_channel("m-900", "", COMP, environment="production")
            c = n.get_or_create_match_channel("", "s-ars-che", COMP, environment="sandbox")
        self.assertTrue(a["created"])
        self.assertFalse(b["created"])
        self.assertEqual(a["channel_id"], b["channel_id"])
        self.assertNotEqual(a["channel_id"], c["channel_id"])  # channels never cross environments
        self.assertEqual([m for m, *_ in calls], ["POST", "POST"])
        self.assertEqual(calls[0][3], {"message-storage-policy": 0, "push-type": "LiveActivity"})

    def test_updates_publish_once_per_channel_and_end_marks_ended(self):
        calls, patcher = self._fake_channel_api()
        with patcher:
            ch = n.get_or_create_match_channel("m-900", "s-ars-che", COMP, environment="production")
        sent = n.send_live_activity_update("m-900", COMP, {"home_score": 1}, event="goal",
                                           alert={"title": "Goal", "body": "x"}, sync_key="s-ars-che")
        self.assertEqual(sent, 1)
        bc = self._broadcasts()
        self.assertEqual(len(bc), 1)
        self.assertEqual(bc[0]["channel_id"], ch["channel_id"])

        # Drain → broadcast HTTP request shape.
        resp = mock.MagicMock(status_code=200, is_success=True, text="", headers={"apns-unique-id": "u1"})
        client = mock.MagicMock()
        client.post.return_value = resp
        with mock.patch.object(n, "_get_apns_client", return_value=client), \
                mock.patch.object(n, "_generate_apns_jwt", return_value="jwt"):
            n._drain_queue()
        url = client.post.call_args[0][0]
        headers = client.post.call_args[1]["headers"]
        body = client.post.call_args[1]["json"]
        self.assertEqual(url, "https://api.push.apple.com/4/broadcasts/apps/com.example.bts")
        self.assertEqual(headers["apns-channel-id"], ch["channel_id"])
        self.assertEqual(headers["apns-push-type"], "liveactivity")
        self.assertEqual(headers["apns-expiration"], "0")
        self.assertEqual(headers["apns-priority"], "10")
        self.assertNotIn("apns-topic", headers)
        self.assertEqual(body["aps"]["event"], "update")

        n.send_live_activity_end("m-900", COMP, {"status": "FT"}, sync_key="s-ars-che")
        self.assertEqual(self._broadcasts()[-1]["event"], "end")
        self.assertTrue(n.find_match_channels("m-900", include_ended=True)[0]["ended_at"])
        self.assertEqual(n.find_match_channels("m-900"), [])  # no further publishes

        # Cleanup after the TTL deletes the APNs channel and drops the record.
        with patcher:
            deleted = n.cleanup_expired_channels(now_ts=time.time() + config.APNS_BROADCAST_DELETE_AFTER_END_SECONDS + 5)
        self.assertEqual(deleted, 1)
        self.assertEqual(calls[-1][0], "DELETE")
        self.assertEqual(n.find_match_channels("m-900", include_ended=True), [])
        self.assertEqual(sqlite_store.load_live_activity_channels(), [])

    def test_push_to_start_uses_input_push_channel_on_ios18(self):
        ko = datetime.now(timezone.utc) + timedelta(minutes=4)
        n.subscribe_team("dev-18", "Arsenal", COMP)
        n.register_push_to_start("pts-18", "dev-18", environment="sandbox")
        n.set_device_la_settings("dev-18", ios_version="18.1")
        self._set_game(status="pre", kickoff_utc=_iso(ko), clock="", period="Scheduled",
                       home_score=None, away_score=None)
        calls, patcher = self._fake_channel_api()
        with patcher:
            self.assertEqual(lp._dispatch_live_activity_starts(), 1)
        start = self._la()[0]
        payload, _ = n.build_live_activity_aps(start)
        self.assertEqual(payload["aps"]["event"], "start")
        self.assertTrue(payload["aps"]["input-push-channel"].startswith("chan-"))
        self.assertNotIn("input-push-token", payload["aps"])
        self.assertEqual(calls[0][1], "sandbox")
        # Server records the channel activity → regular pushes suppressed for that device.
        self.assertIn("dev-18", n.live_activity_device_tokens("m-900", COMP))

    def test_ios17_device_still_gets_input_push_token(self):
        ko = datetime.now(timezone.utc) + timedelta(minutes=4)
        n.subscribe_team("dev-17", "Arsenal", COMP)
        n.register_push_to_start("pts-17", "dev-17")
        n.set_device_la_settings("dev-17", ios_version="17.4")
        self._set_game(status="pre", kickoff_utc=_iso(ko), clock="", period="Scheduled",
                       home_score=None, away_score=None)
        lp._dispatch_live_activity_starts()
        payload, _ = n.build_live_activity_aps(self._la()[0])
        self.assertEqual(payload["aps"]["input-push-token"], 1)


class EndpointTests(_Base):
    def setUp(self):
        super().setUp()
        self.client = TestClient(app, raise_server_exceptions=False)

    def test_channel_endpoint_disabled_unknown_and_success(self):
        res = self.client.post("/api/live-activities/channel", json={"match_id": "m-900"})
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.json()["fallback"], "token")

        config.APNS_BROADCAST_ENABLED = True
        with mock.patch.object(sqlite_store, "get_game_by_match_id", return_value=None):
            res = self.client.post("/api/live-activities/channel", json={"match_id": "nope-1"})
        self.assertEqual(res.status_code, 404)

        self._set_game(status="pre", clock="", period="Scheduled")
        with mock.patch.object(n, "_apns_channel_request",
                               return_value={"ok": True, "status_code": 201, "channel_id": "abc=="}):
            res = self.client.post("/api/live-activities/channel",
                                   json={"match_id": "m-900", "environment": "sandbox"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["channel_id"], "abc==")
        self.assertEqual(res.headers.get("cache-control"), "no-store")

    def test_channel_activity_registration_suppresses_regular_push(self):
        n.subscribe_match("dev-ch", "m-900", COMP)
        res = self.client.post("/api/live-activities/register", json={
            "channel_id": "abc==", "device_token": "dev-ch", "match_id": "m-900", "sync_key": "s-ars-che",
        })
        self.assertEqual(res.json()["mode"], "channel")
        self.assertEqual(
            n.for_game_or_team_subscribers("m-900", COMP, exclude_live_activity_devices=True, sync_key="s-ars-che"),
            [],
        )
        res = self.client.post("/api/live-activities/unregister", json={
            "device_token": "dev-ch", "match_id": "m-900", "sync_key": "s-ars-che",
        })
        self.assertTrue(res.json()["removed"])
        self.assertEqual(n.live_activity_device_tokens("m-900", COMP, sync_key="s-ars-che"), set())

    def test_settings_endpoint_and_register_fields(self):
        res = self.client.post("/api/live-activities/settings", json={
            "device_token": "dev-s", "frequent_updates_enabled": False, "ios_version": "18.0",
        })
        self.assertEqual(res.json()["settings"]["frequent_updates"], False)
        self.assertTrue(res.json()["settings"]["supports_broadcast"])
        res = self.client.get("/api/live-activities/settings?device_token=dev-s")
        self.assertFalse(res.json()["settings"]["frequent_updates"])

        res = self.client.post("/api/notifications/register", json={
            "token": "dev-r", "activities_enabled": False, "events": ["goal"],
        })
        body = res.json()
        self.assertFalse(body["live_activity_settings"]["activities_enabled"])
        self.assertEqual(body["events"], sorted(n.ALL_EVENT_TYPES))  # all-or-none

    def test_startup_returns_todays_upcoming_games(self):
        today = datetime.now(timezone.utc).astimezone(
            __import__("zoneinfo").ZoneInfo("America/New_York")
        ).date().isoformat()
        row = {"match_date_iso": today, "competition": COMP, "home_team": "Arsenal",
               "away_team": "Chelsea", "sync_key": "s-ars-che"}
        import predictions as pred_mod
        import routes.live_scores as ls
        with mock.patch.object(pred_mod, "_load_upcoming_rows", return_value=([row], None, None)), \
                mock.patch.object(ls, "regional_espn_schedule_fallback", side_effect=lambda rows: rows):
            res = self.client.get("/api/startup")
        upcoming = res.json()["upcoming_today"]
        self.assertGreaterEqual(upcoming["total_games"], 1)


if __name__ == "__main__":
    unittest.main()
