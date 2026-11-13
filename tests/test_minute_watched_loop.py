from unittest.mock import patch

from TwitchChannelPointsMiner.classes.Twitch import Twitch

MODULE = "TwitchChannelPointsMiner.classes.Twitch"


def _twitch():
    twitch = object.__new__(Twitch)
    twitch.running = True
    return twitch


def test_each_loop_runs_one_pass_with_the_watch_settings():
    twitch = _twitch()
    calls = []

    def one_pass(*args):
        calls.append(args)
        twitch.running = False

    with (
        patch.object(Twitch, "_Twitch__send_minute_watched_pass", side_effect=one_pass),
        patch.object(Twitch, "_Twitch__chuncked_sleep") as sleep,
    ):
        twitch.send_minute_watched_events(
            ["streamer"],
            ["priority"],
            chunk_size=4,
            streams_watched=1,
            source_priority=["source"],
            drop_progress_stall_minutes=7,
            drop_pick_stickiness_minutes=3,
            watch_status_interval_minutes=9,
        )

    assert calls == [(["streamer"], ["priority"], 4, 1, ["source"], 7, 3, 9)]
    sleep.assert_called_once()
    assert sleep.call_args.kwargs == {"chunk_size": 4}


def test_a_failing_pass_is_logged_and_the_loop_continues():
    twitch = _twitch()
    passes = []

    def one_pass(*_args):
        passes.append(True)
        if len(passes) == 1:
            raise RuntimeError("boom")
        twitch.running = False

    with (
        patch.object(Twitch, "_Twitch__send_minute_watched_pass", side_effect=one_pass),
        patch.object(Twitch, "_Twitch__chuncked_sleep") as sleep,
        patch(f"{MODULE}.logger") as logger,
    ):
        twitch.send_minute_watched_events([], [])

    assert len(passes) == 2
    assert sleep.call_count == 2
    logger.error.assert_called_once()
    assert logger.error.call_args.kwargs["exc_info"] is True
