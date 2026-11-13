"""DropAnalytics on its own: plain state, files under tmp_path, no Twitch (#280)."""

import json
from threading import RLock
from types import SimpleNamespace

import pytest

from TwitchChannelPointsMiner.classes.DropAnalytics import DropAnalytics
from TwitchChannelPointsMiner.classes.Settings import Settings, StreamerSource
from TwitchChannelPointsMiner.data_migration import ANALYTICS_DATA_VERSION


@pytest.fixture
def analytics_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(Settings, "enable_analytics", True, raising=False)
    monkeypatch.setattr(Settings, "analytics_path", str(tmp_path), raising=False)
    return tmp_path


def _analytics(report_state=None, track_item_art=False):
    return DropAnalytics(
        RLock(),
        {} if report_state is None else report_state,
        {},
        {},
        track_item_art=track_item_art,
    )


def _drop(minutes=30, drop_id="drop-1", name="Reward"):
    return SimpleNamespace(
        id=drop_id,
        name=name,
        benefit=name,
        current_minutes_watched=minutes,
        minutes_required=60,
        percentage_progress=minutes * 100 // 60,
        end_at=None,
        item_art_url="https://example.invalid/art.png",
    )


def _campaign():
    return SimpleNamespace(name="Campaign", game={"displayName": "Example Game"})


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_save_now_watching_writes_each_stream_with_its_reason(analytics_dir):
    streamers = [
        SimpleNamespace(
            username="points_channel",
            channel_points=100,
            stream=SimpleNamespace(game={"displayName": "Example Game"}),
        ),
        SimpleNamespace(
            username="drops_channel",
            channel_points=5,
            stream=SimpleNamespace(game=None),
        ),
    ]
    sources = {0: StreamerSource.STREAMERS, 1: StreamerSource.CATEGORIES}

    _analytics().save_now_watching(streamers, [1, 0], sources.get)

    assert _read(analytics_dir / "now_watching.json") == [
        {
            "username": "drops_channel",
            "reason": "drops",
            "game": None,
            "channel_points": 5,
        },
        {
            "username": "points_channel",
            "reason": "points",
            "game": "Example Game",
            "channel_points": 100,
        },
    ]


def test_progress_then_claim_keeps_one_row_per_drop(analytics_dir):
    analytics = _analytics()

    analytics.save_drop_progress(_drop(30), campaign=_campaign(), streamer_username="a")
    analytics.save_drop(_drop(60), campaign=_campaign(), streamer_username="a")

    data = _read(analytics_dir / "drops_by_category.json")
    assert data["version"] == ANALYTICS_DATA_VERSION
    assert len(data["drops"]) == 1
    row = data["drops"][0]
    assert row["status"] == "captured"
    assert row["current_minutes_watched"] == 60
    assert row["category"] == "Example Game"
    # Item art is only kept when tracking it is on.
    assert row["item_art_url"] is None
    assert list(analytics.report_snapshot().values())[0]["status"] == "captured"


def test_uncategorized_drop_is_not_recorded(analytics_dir):
    analytics = _analytics()

    analytics.save_drop(_drop())

    assert analytics.report_snapshot() == {}
    assert not (analytics_dir / "drops_by_category.json").exists()


def test_report_state_is_kept_when_analytics_files_are_off(tmp_path, monkeypatch):
    monkeypatch.setattr(Settings, "enable_analytics", False, raising=False)
    monkeypatch.setattr(Settings, "analytics_path", str(tmp_path), raising=False)
    analytics = _analytics()

    analytics.save_drop(_drop(), campaign=_campaign())

    assert len(analytics.report_snapshot()) == 1
    assert list(tmp_path.iterdir()) == []


def test_restore_report_state_does_not_replace_newer_entries():
    state = {"kept": {"status": "captured"}}
    analytics = _analytics(report_state=state)

    analytics.restore_report_state(
        {"kept": {"status": "in_progress"}, "added": {"status": "in_progress"}, "x": 1}
    )

    assert state == {
        "kept": {"status": "captured"},
        "added": {"status": "in_progress"},
    }


def test_snapshot_is_written_only_when_progress_or_final_status_changes():
    analytics = _analytics()

    assert analytics.should_save_snapshot("key", 10, "in_progress") is True
    analytics.mark_snapshot_saved("key", 10, "in_progress")
    assert analytics.should_save_snapshot("key", 10, "in_progress") is False
    assert analytics.should_save_snapshot("key", 11, "in_progress") is True
    assert analytics.should_save_snapshot("key", 10, "captured") is True


def test_awarded_game_event_drops_mark_matching_rows_captured(analytics_dir):
    analytics = _analytics()
    analytics.save_drop_progress(_drop(30), campaign=_campaign())

    updated = analytics.reconcile_awarded_game_event_drops(
        {"gameEventDrops": [{"name": "Reward", "lastAwardedAt": "2026-11-01"}]}
    )

    assert updated == 1
    row = _read(analytics_dir / "drops_by_category.json")["drops"][0]
    assert row["status"] == "captured"
    assert row["current_minutes_watched"] == 60
    assert row["percentage_progress"] == 100
    assert DropAnalytics.captured_history() == [row]
