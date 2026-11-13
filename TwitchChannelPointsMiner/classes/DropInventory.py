"""Read the account's Drops inventory (#280).

DropInventory wraps one inventory reply (the Inventory GQL query) and works
out what it says: which drops and campaigns are finished, which finished
campaigns reopened, and the minutes the account has on each unfinished drop.
It sends no requests and changes no shared state; Twitch stores what it
returns and decides when the snapshot is too old to use.
"""

import copy
from dataclasses import dataclass

from TwitchChannelPointsMiner.classes.entities.Campaign import CampaignChannelScope
from TwitchChannelPointsMiner.classes.entities.Drop import (
    drop_required_subs,
    drop_sub_requirement_unmet,
)


@dataclass(frozen=True, slots=True)
class InventoryProgress(object):
    """The unfinished drops of one inventory reply."""

    # Game slug -> sorted (campaign ID, campaign name, drop ID, drop name,
    # current minutes, required minutes, allowed channels or None).
    by_game: dict
    # Drop ID -> minutes watched, for the unfinished drops above.
    minutes_by_drop_id: dict
    # IDs of drops that are claimed, or watched in full and claimable.
    finished_ids: frozenset


class DropInventory(object):
    __slots__ = ["inventory"]

    def __init__(self, inventory: dict):
        self.inventory = inventory

    def claimed_drop_ids(self) -> set:
        inventory = self.inventory
        completed_ids = set()

        for campaign in inventory.get("dropCampaignsInProgress", []) or []:
            if not isinstance(campaign, dict):
                continue
            for drop in campaign.get("timeBasedDrops", []) or []:
                if not isinstance(drop, dict):
                    continue
                drop_self = drop.get("self") or {}
                if drop_self.get("isClaimed") is True and drop.get("id"):
                    completed_ids.add(str(drop["id"]))

        return completed_ids

    def completed_campaign_ids(self) -> set:
        inventory = self.inventory
        completed_ids = set()

        for completed_record in inventory.get("completedRewardCampaigns", []) or []:
            if not isinstance(completed_record, dict):
                continue
            _, campaign_id = self.completed_record_campaign(completed_record)
            if campaign_id is not None:
                completed_ids.add(campaign_id)

        # Twitch can briefly leave a completed campaign in the in-progress
        # collection before moving it to completedRewardCampaigns. All Drops
        # being explicitly claimed is also authoritative completion evidence,
        # as is every drop having already accrued its required watch time:
        # claiming is an inventory call that does not need more watching, so a
        # fully captured but not-yet-claimed campaign must not stay watchable.
        for campaign in inventory.get("dropCampaignsInProgress", []) or []:
            if not isinstance(campaign, dict):
                continue
            campaign_id = campaign.get("id")
            drops = campaign.get("timeBasedDrops", []) or []
            if campaign_id in [None, ""] or not drops:
                continue
            if all(
                isinstance(drop, dict)
                and isinstance(drop.get("self"), dict)
                and self.drop_is_done(drop)
                for drop in drops
            ):
                completed_ids.add(str(campaign_id))

        return completed_ids

    def reopened_campaign_ids(self) -> set:
        inventory = self.inventory
        # A campaign Twitch lists in progress with an unfinished drop has gained
        # work since it was marked completed, for example a new drop added to a
        # finished campaign. Completion that Twitch reports itself stays final.
        completed_ids = self.completed_campaign_ids()
        reopened_ids = set()
        for campaign in inventory.get("dropCampaignsInProgress", []) or []:
            if not isinstance(campaign, dict):
                continue
            campaign_id = campaign.get("id")
            if campaign_id in [None, ""]:
                continue
            campaign_id = str(campaign_id)
            if campaign_id in completed_ids:
                continue
            for drop in campaign.get("timeBasedDrops", []) or []:
                if (
                    isinstance(drop, dict)
                    and isinstance(drop.get("self"), dict)
                    and not self.drop_is_done(drop)
                ):
                    reopened_ids.add(campaign_id)
                    break
        return reopened_ids

    def progress(self, slugify, campaign_scope, accepted_claims) -> InventoryProgress:
        """The account's unfinished drops, grouped by game slug.

        ``campaign_scope(campaign_id, campaign)`` returns the campaign's
        channel scope and logins; ``accepted_claims`` holds the drop instance
        IDs whose claims Twitch accepted.
        """
        inventory = self.inventory
        progress_by_game = {}
        finished_ids = set()
        for campaign in inventory.get("dropCampaignsInProgress", []) or []:
            if not isinstance(campaign, dict):
                continue
            game = campaign.get("game") or {}
            game_name = (game.get("displayName") or game.get("name") or "").strip()
            game_slug = slugify(game_name)
            if game_slug == "":
                continue

            campaign_id = str(campaign.get("id") or "")
            campaign_name = str(campaign.get("name") or "Unknown campaign")
            # The allow-listed channels, or None when any channel counts. A
            # record without allow data takes the campaign's last known
            # scope; failing that, it is still the account's own progress,
            # so it counts for every channel, as before, rather than holding
            # no pick at all.
            scope, channel_logins = campaign_scope(campaign_id, campaign)
            allowed_channels = (
                tuple(sorted(channel_logins))
                if scope is CampaignChannelScope.RESTRICTED
                else None
            )
            for drop in campaign.get("timeBasedDrops", []) or []:
                if not isinstance(drop, dict):
                    continue
                drop_self = drop.get("self")
                if not isinstance(drop_self, dict):
                    continue
                required_minutes = drop.get("requiredMinutesWatched") or 0
                current_minutes = drop_self.get("currentMinutesWatched") or 0
                instance_id = drop_self.get("dropInstanceID")
                # An accepted claim counts as claimed; the inventory is lagging.
                claimed = (
                    drop_self.get("isClaimed") is True or instance_id in accepted_claims
                )
                # A fully watched drop counts as finished only once it can be
                # claimed: until Twitch issues its instance, it may still need
                # watching.
                if drop.get("id") and (
                    claimed or (instance_id and 0 < required_minutes <= current_minutes)
                ):
                    finished_ids.add(str(drop["id"]))
                if claimed or drop_self.get("hasPreconditionsMet") is False:
                    continue

                if required_minutes <= 0 or current_minutes >= required_minutes:
                    continue
                # Watching alone cannot earn it until the account subscribes.
                if drop_sub_requirement_unmet(drop):
                    continue

                progress_by_game.setdefault(game_slug, []).append(
                    (
                        campaign_id,
                        campaign_name,
                        str(drop.get("id") or ""),
                        str(drop.get("name") or "Unknown drop"),
                        current_minutes,
                        required_minutes,
                        allowed_channels,
                    )
                )

        return InventoryProgress(
            by_game={
                game_slug: tuple(sorted(progress))
                for game_slug, progress in progress_by_game.items()
            },
            minutes_by_drop_id={
                drop_id: current
                for progress in progress_by_game.values()
                for (_, _, drop_id, _, current, _, _) in progress
                if drop_id
            },
            finished_ids=frozenset(finished_ids),
        )

    @staticmethod
    def completed_record_campaign(completed_record):
        campaign = completed_record.get("campaign")
        if not isinstance(campaign, dict):
            campaign = completed_record
        campaign_id = campaign.get("id")
        if campaign_id in [None, ""] and isinstance(campaign.get("campaign"), dict):
            campaign_id = campaign["campaign"].get("id")
        return campaign, None if campaign_id in [None, ""] else str(campaign_id)

    @staticmethod
    def drop_is_done(drop: dict) -> bool:
        drop_self = drop.get("self")
        if not isinstance(drop_self, dict):
            return False
        if drop_self.get("isClaimed") is True:
            return True
        required_minutes = drop.get("requiredMinutesWatched") or 0
        return (
            required_minutes > 0
            and (drop_self.get("currentMinutesWatched") or 0) >= required_minutes
        )

    @staticmethod
    def merge_campaign_progress(campaign: dict, inventory_campaign: dict) -> dict:
        """Keep fresh campaign drops with authenticated inventory state and access."""
        if not campaign.get("timeBasedDrops"):
            merged_campaign = copy.deepcopy(inventory_campaign)
            # The fresh record's allow-list still applies when the inventory
            # carries none.
            if not isinstance(merged_campaign.get("allow"), dict) and isinstance(
                campaign.get("allow"), dict
            ):
                merged_campaign["allow"] = copy.deepcopy(campaign["allow"])
            return merged_campaign

        merged_campaign = copy.deepcopy(campaign)
        if isinstance(inventory_campaign.get("allow"), dict):
            merged_campaign["allow"] = copy.deepcopy(inventory_campaign["allow"])
        if not inventory_campaign.get("timeBasedDrops"):
            return merged_campaign

        merged_drops = merged_campaign.get("timeBasedDrops", [])
        merged_drop_ids = {
            str(drop.get("id"))
            for drop in merged_drops
            if isinstance(drop, dict) and drop.get("id") not in [None, ""]
        }
        inventory_drops = {
            str(drop.get("id")): drop
            for drop in inventory_campaign.get("timeBasedDrops", []) or []
            if isinstance(drop, dict) and drop.get("id") not in [None, ""]
        }

        for drop in merged_drops:
            if not isinstance(drop, dict) or drop.get("id") in [None, ""]:
                continue
            inventory_drop = inventory_drops.get(str(drop["id"]))
            if isinstance(inventory_drop, dict) and isinstance(
                inventory_drop.get("self"), dict
            ):
                drop["self"] = inventory_drop["self"]

        for drop_id, drop in inventory_drops.items():
            if drop_id not in merged_drop_ids:
                merged_drops.append(copy.deepcopy(drop))

        return merged_campaign

    @staticmethod
    def with_progress(campaign: dict, minutes_by_drop_id: dict) -> dict:
        """Return the campaign with in-progress minutes filled in for the drops
        in ``minutes_by_drop_id`` that carry no per-account "self" data."""
        changed = False
        drops = []
        for drop in campaign.get("timeBasedDrops", []) or []:
            drop_id = str(drop.get("id") or "") if isinstance(drop, dict) else ""
            if drop_id in minutes_by_drop_id and not isinstance(drop.get("self"), dict):
                drop_self = {
                    "isClaimed": False,
                    "hasPreconditionsMet": True,
                    "currentMinutesWatched": minutes_by_drop_id[drop_id],
                }
                required_subs = drop_required_subs(drop)
                if required_subs:
                    # The snapshot keeps only drops whose subscription
                    # requirement the account had met (see
                    # __cache_drop_inventory_progress).
                    drop_self["currentSubs"] = required_subs
                drop = {**drop, "self": drop_self}
                changed = True
            drops.append(drop)
        if not changed:
            return campaign
        return {**campaign, "timeBasedDrops": drops}

    @staticmethod
    def progress_label(progress) -> str:
        labels = [
            f"{campaign_name} / {drop_name} ({current}/{required}m)"
            for _, campaign_name, _, drop_name, current, required, _ in progress[:3]
        ]
        if len(progress) > 3:
            labels.append(f"{len(progress) - 3} more")
        return ", ".join(labels)
