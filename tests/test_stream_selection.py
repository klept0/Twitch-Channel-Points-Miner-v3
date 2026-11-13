"""StreamSelector on its own: plain stand-ins and callables, no Twitch (#280)."""

from datetime import timedelta
from types import SimpleNamespace

from TwitchChannelPointsMiner.classes.entities.Drop import utc_now
from TwitchChannelPointsMiner.classes.Settings import Priority, StreamerSource
from TwitchChannelPointsMiner.classes.StreamSelection import StreamSelector

NOW = 1_000_000.0


def _streamer(username, game="Just Chatting", online=True, **flags):
    return SimpleNamespace(
        username=username,
        is_online=online,
        online_at=0,
        channel_points=0,
        settings=SimpleNamespace(favorite=False),
        stream=SimpleNamespace(
            game_name=lambda: game,
            campaigns=[],
            channel_campaign_ids=[],
            broadcast_id=None,
        ),
        **flags,
    )


def _slugify(name):
    return name.lower().replace(" ", "-")


def _selector(drop_streamers=(), points_limit_reached=(), **state):
    return StreamSelector(
        drops_condition=lambda streamer: streamer.username in drop_streamers,
        previous_pick_still_farming=lambda streamer: False,
        has_reached_points_limit=(
            lambda streamer: streamer.username in points_limit_reached
        ),
        has_pending_watch_streak=lambda streamer, now: False,
        in_progress_drop_needs=lambda game_slug, username: None,
        slugify=_slugify,
        **state,
    )


def _usernames(streamers, selection):
    return [streamers[index].username for index in selection.watching]


def test_order_priority_skips_offline_and_capped_streamers():
    streamers = [
        _streamer("offline", online=False),
        _streamer("capped"),
        _streamer("first"),
        _streamer("second"),
        _streamer("third"),
    ]

    selection = _selector(points_limit_reached={"capped"}).select(
        streamers, NOW, [Priority.ORDER], 2
    )

    assert _usernames(streamers, selection) == ["first", "second"]
    assert selection.streamer_source(2) is StreamerSource.STREAMERS
    assert selection.drop_pick_streamer is None
    assert selection.drop_conflict_key is None
    assert selection.drop_conflict_message is None


def test_stalled_drop_progress_is_skipped():
    streamers = [_streamer("stalled", game="Rust"), _streamer("other")]

    selection = _selector().select(
        streamers,
        NOW,
        [Priority.ORDER],
        2,
        drop_progress_cooldowns={("rust", "stalled")},
    )

    assert _usernames(streamers, selection) == ["other"]


def _two_category_streams():
    deadlines = {
        "soon": utc_now() + timedelta(minutes=60),
        "later": utc_now() + timedelta(minutes=70),
    }
    streamers = [
        _streamer("soon_channel", game="Soon", from_category=True),
        _streamer("later_channel", game="Later", from_category=True),
    ]
    return streamers, deadlines


def test_only_the_soonest_expiring_category_stream_is_watched():
    streamers, deadlines = _two_category_streams()

    selection = _selector(
        drop_streamers={"soon_channel", "later_channel"},
        category_deadlines=deadlines,
    ).select(streamers, NOW, [Priority.ORDER], 2)

    assert _usernames(streamers, selection) == ["soon_channel"]
    assert selection.category.watched_pick == 0
    assert selection.category.ordered == [0, 1]
    assert selection.category.reason is None
    assert selection.drop_pick_streamer == "soon_channel"


def test_previous_pick_keeps_the_slot_within_the_stickiness_margin():
    streamers, deadlines = _two_category_streams()

    selection = _selector(
        drop_streamers={"soon_channel", "later_channel"},
        category_deadlines=deadlines,
        previous_drop_pick="later_channel",
    ).select(
        streamers,
        NOW,
        [Priority.ORDER],
        2,
        drop_pick_stickiness_minutes=15,
    )

    assert _usernames(streamers, selection) == ["later_channel"]
    assert selection.category.watched_pick == 1
    assert selection.category.reason.startswith("held by stickiness")
    assert selection.drop_pick_streamer == "later_channel"


def test_a_configured_drops_stream_outranks_the_category_pick():
    streamers = [
        _streamer("configured", game="Rust", explicitly_configured=True),
        _streamer("category", game="Rust", from_category=True),
    ]

    selection = _selector(drop_streamers={"configured", "category"}).select(
        streamers, NOW, [Priority.ORDER], 2
    )

    # Drops progress counts on one channel only, so the category stream
    # is evicted and no category pick is remembered.
    assert _usernames(streamers, selection) == ["configured"]
    assert selection.category.watched_pick is None
    assert selection.drop_pick_streamer is None
    assert selection.drop_conflict_key == (False, "configured", ("category",))
    assert selection.drop_conflict_message == (
        "Drops progress only counts on one channel at a time: "
        "keeping configured (streamers), skipping category (categories)"
    )


def test_select_leaves_the_given_state_unchanged():
    streamers, deadlines = _two_category_streams()
    revoked = set()
    before = dict(deadlines)

    _selector(
        drop_streamers={"soon_channel", "later_channel"},
        category_deadlines=deadlines,
        drop_cover_revoked=revoked,
    ).select(streamers, NOW, [Priority.ORDER], 2)

    assert deadlines == before
    assert revoked == set()
    assert not any(hasattr(streamer, "is_watching") for streamer in streamers)
