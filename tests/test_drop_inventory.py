"""DropInventory on its own: inventory payloads in, plain values out (#280)."""

from TwitchChannelPointsMiner.classes.DropInventory import DropInventory
from TwitchChannelPointsMiner.classes.entities.Campaign import CampaignChannelScope


def _drop(drop_id, current, required=60, claimed=False, instance=None, **extra):
    return {
        "id": drop_id,
        "name": f"Drop {drop_id}",
        "requiredMinutesWatched": required,
        "self": {
            "currentMinutesWatched": current,
            "isClaimed": claimed,
            "hasPreconditionsMet": True,
            "dropInstanceID": instance,
        },
        **extra,
    }


def _campaign(campaign_id, *drops, game="Example Game"):
    return {
        "id": campaign_id,
        "name": f"Campaign {campaign_id}",
        "game": {"displayName": game},
        "timeBasedDrops": list(drops),
    }


def _inventory(*campaigns, completed=()):
    return {
        "dropCampaignsInProgress": list(campaigns),
        "completedRewardCampaigns": [{"id": cid} for cid in completed],
    }


def _any_channel(campaign_id, campaign):
    return CampaignChannelScope.OPEN, []


def test_progress_lists_unfinished_drops_by_game():
    inventory = _inventory(
        _campaign(
            "c1",
            _drop("watching", 20),
            _drop("claimable", 60, instance="i-1"),
            _drop("claimed", 60, claimed=True),
            _drop("accepted", 30, instance="i-2"),
        )
    )

    progress = DropInventory(inventory).progress(
        lambda name: name.lower().replace(" ", "-"), _any_channel, {"i-2": True}
    )

    assert progress.by_game == {
        "example-game": (
            ("c1", "Campaign c1", "watching", "Drop watching", 20, 60, None),
        )
    }
    assert progress.minutes_by_drop_id == {"watching": 20}
    assert progress.finished_ids == frozenset({"claimable", "claimed", "accepted"})


def test_claimed_drop_ids_are_only_the_claimed_ones():
    inventory = _inventory(
        _campaign("c1", _drop("claimed", 60, claimed=True), _drop("open", 60))
    )

    assert DropInventory(inventory).claimed_drop_ids() == {"claimed"}


def test_completed_and_reopened_campaigns():
    inventory = _inventory(
        _campaign("watched-out", _drop("a", 60), _drop("b", 60, claimed=True)),
        _campaign("new-drop", _drop("c", 60, claimed=True), _drop("d", 0)),
        completed=["reported"],
    )
    drop_inventory = DropInventory(inventory)

    assert drop_inventory.completed_campaign_ids() == {"reported", "watched-out"}
    assert drop_inventory.reopened_campaign_ids() == {"new-drop"}


def test_with_progress_fills_only_drops_without_account_data():
    campaign = {
        "id": "c1",
        "timeBasedDrops": [
            {"id": "listed", "requiredMinutesWatched": 60},
            _drop("own", 5),
        ],
    }

    overlaid = DropInventory.with_progress(campaign, {"listed": 40, "own": 50})

    assert overlaid["timeBasedDrops"][0]["self"] == {
        "isClaimed": False,
        "hasPreconditionsMet": True,
        "currentMinutesWatched": 40,
    }
    assert overlaid["timeBasedDrops"][1]["self"]["currentMinutesWatched"] == 5
    # The input campaign is left as it was.
    assert "self" not in campaign["timeBasedDrops"][0]
    assert DropInventory.with_progress(campaign, {}) is campaign


def test_merge_keeps_fresh_drops_with_inventory_progress():
    fresh = _campaign("c1", {"id": "a", "requiredMinutesWatched": 60})
    inventory_campaign = _campaign("c1", _drop("a", 25), _drop("b", 10))

    merged = DropInventory.merge_campaign_progress(fresh, inventory_campaign)

    assert [drop["id"] for drop in merged["timeBasedDrops"]] == ["a", "b"]
    assert merged["timeBasedDrops"][0]["self"]["currentMinutesWatched"] == 25
    assert "self" not in fresh["timeBasedDrops"][0]


def test_progress_label_names_three_drops_and_counts_the_rest():
    progress = [("c", "Campaign", "id", f"Drop {n}", n, 60, None) for n in range(1, 6)]

    assert DropInventory.progress_label(progress) == (
        "Campaign / Drop 1 (1/60m), Campaign / Drop 2 (2/60m), "
        "Campaign / Drop 3 (3/60m), 2 more"
    )
