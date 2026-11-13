"""DropClaims on its own, with fakes for what Twitch passes in (#280).

The claim rules themselves are covered through Twitch in
test_twitch_drop_claim.py; these tests pin the seams: what DropClaims asks
of its collaborators, and that Twitch wires them up late-bound.
"""

import logging
from types import SimpleNamespace

import pytest

import TwitchChannelPointsMiner.classes.DropClaims as drop_claims_module
from TwitchChannelPointsMiner.classes.DropClaims import DropClaims
from TwitchChannelPointsMiner.classes.Twitch import Twitch


class Recorder(object):
    """The collaborators DropClaims takes, recording every call."""

    def __init__(self, status="ELIGIBLE_FOR_ALL", connected=None, inventory=None):
        self.claimed = []
        self.forgotten = []
        self.saved = []
        self.checks = []
        self.status = status
        self.connected = connected
        self.inventory = inventory or {}

    def claim_rewards(self, drop_instance_id):
        self.claimed.append(drop_instance_id)
        return SimpleNamespace(
            status=self.status,
            errors=[],
            is_user_account_connected=self.connected,
        )

    def drop_analytics(self):
        recorder = self
        return SimpleNamespace(
            variant_entries=lambda drop: [{"name": None}],
            save_drop=lambda drop, **kwargs: recorder.saved.append(drop.id),
        )

    def claims(self):
        return DropClaims(
            claim_rewards=self.claim_rewards,
            get_inventory=lambda: self.inventory,
            drop_analytics=self.drop_analytics,
            log_drop_check=lambda message, level=logging.INFO: self.checks.append(
                message
            ),
            forget_inventory_minutes=self.forgotten.append,
        )


def drop(drop_id="drop-1", instance_id="instance-1"):
    return SimpleNamespace(id=drop_id, drop_instance_id=instance_id, name="Reward")


def test_accepted_claim_is_recorded_saved_and_forgets_inventory_minutes():
    recorder = Recorder()
    claims = recorder.claims()
    reward = drop()

    assert claims.claim_drop(reward) is True

    assert recorder.claimed == ["instance-1"]
    assert recorder.saved == ["drop-1"]
    assert recorder.forgotten == [reward]
    assert claims.accepted_drop_ids == frozenset({"drop-1"})
    assert set(claims.accepted_drop_claims) == {"instance-1"}
    assert claims.claim_requests_sent == 1


def test_unlinked_claim_is_not_saved_and_appears_in_the_report():
    recorder = Recorder(connected=False)
    claims = recorder.claims()
    reward = drop()

    assert (
        claims.claim_drop(
            reward,
            campaign_name_override="Campaign",
            category_name_override="Game",
            campaign_link={"campaign_id": "campaign-1", "url": "https://x.test"},
        )
        is False
    )

    assert recorder.saved == []
    assert claims.accepted_drop_claims == {}
    assert claims.accepted_drop_ids == frozenset({"drop-1"})
    assert [group["campaign_id"] for group in claims.unlinked_drop_report()] == [
        "campaign-1"
    ]


def test_claim_pass_reads_the_inventory_and_paces_only_sent_claims(monkeypatch):
    inventory_drop = {
        "id": "drop-1",
        "name": "Reward",
        "self": {
            "hasPreconditionsMet": True,
            "currentMinutesWatched": 10,
            "dropInstanceID": "instance-1",
            "isClaimed": False,
        },
        "requiredMinutesWatched": 10,
        "startAt": "2020-01-01T00:00:00Z",
        "endAt": "2099-01-01T00:00:00Z",
    }
    recorder = Recorder(
        inventory={
            "dropCampaignsInProgress": [
                {
                    "id": "campaign-1",
                    "name": "Campaign",
                    "game": {"displayName": "Game"},
                    "timeBasedDrops": [inventory_drop],
                }
            ]
        }
    )
    claims = recorder.claims()
    sleeps = []
    monkeypatch.setattr(drop_claims_module.time, "sleep", sleeps.append)
    monkeypatch.setattr(drop_claims_module.random, "uniform", lambda low, high: low)

    claims.claim_all_drops_from_inventory()
    # Accepted an hour ago at most: the second pass sends nothing.
    claims.claim_all_drops_from_inventory()

    assert recorder.claimed == ["instance-1"]
    assert sleeps == [5]


def test_campaign_link_from_inventory_reads_the_self_edge():
    assert DropClaims.campaign_link_from_inventory(
        {
            "id": "campaign-1",
            "self": {"isAccountConnected": False},
            "accountLinkURL": "https://x.test",
        }
    ) == {"campaign_id": "campaign-1", "connected": False, "url": "https://x.test"}
    assert DropClaims.campaign_link_from_inventory(None) is None


def test_twitch_creates_its_drop_claims_once_and_late_binds_gql():
    twitch = object.__new__(Twitch)
    twitch.log_drop_checks = False

    claims = twitch.drop_claims
    # Set after drop_claims exists, as tests and login both do.
    twitch.gql = SimpleNamespace(
        claim_drop_rewards=lambda instance_id: SimpleNamespace(
            status="ELIGIBLE_FOR_ALL", errors=[], is_user_account_connected=None
        )
    )

    assert twitch.drop_claims is claims
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            Twitch,
            "_Twitch__drop_analytics",
            lambda self: SimpleNamespace(variant_entries=lambda drop: []),
        )
        assert twitch.claim_drop(drop()) is True
    assert claims.accepted_drop_ids == frozenset({"drop-1"})
