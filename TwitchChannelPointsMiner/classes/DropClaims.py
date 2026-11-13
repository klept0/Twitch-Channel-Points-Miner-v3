"""Claim earned Drops and keep track of the claims (#280).

DropClaims sends Drop claims and remembers what Twitch answered: claims it
accepted, claims it accepted but won't deliver until the campaign's game
account is linked, and the debounce of prompt claims for a drop that just
reached 100%. Twitch creates one and passes in what it needs from the rest
of the miner (the claim request, the inventory, analytics and drop-check
logging), so the claim rules can be tested on their own.
"""

import logging
import random
import time
from threading import Lock, Thread
from typing import Any, Callable

from TwitchChannelPointsMiner.classes.entities.Drop import Drop
from TwitchChannelPointsMiner.classes.gql.Errors import RetryError
from TwitchChannelPointsMiner.classes.Settings import Events

logger = logging.getLogger(__name__)

# A drop that just reached 100% gets one prompt claim pass in this window.
PROMPT_CLAIM_DEBOUNCE_SECONDS = 300
# Twitch can accept a claim while the inventory keeps listing the drop as
# unclaimed. Wait this long before claiming the same drop instance again.
ACCEPTED_CLAIM_RETRY_SECONDS = 60 * 60
# A claim Twitch accepted without delivering the reward, because the
# campaign's game account is not linked. The inventory reporting the account
# as linked retries it at once; this is only the fallback.
UNLINKED_CLAIM_RETRY_SECONDS = 6 * 60 * 60
# How long an unlinked claim's drop may be missing from the inventory before
# the claim is forgotten: long enough to ride out short or empty responses,
# short enough that an ended campaign leaves the daily report the same day.
UNLINKED_CLAIM_MISSING_GRACE_SECONDS = 15 * 60


class DropClaims(object):
    __slots__ = [
        "claim_lock",
        "accepted_drop_ids",
        "accepted_drop_claims",
        "unlinked_drop_claims",
        "claim_requests_sent",
        "prompt_claim_lock",
        "prompt_claim_pass_lock",
        "prompt_claim_last",
        "_claim_rewards",
        "_get_inventory",
        "_drop_analytics",
        "_log_drop_check",
        "_forget_inventory_minutes",
    ]

    def __init__(
        self,
        claim_rewards: Callable[[str], Any],
        get_inventory: Callable[[], Any],
        drop_analytics: Callable[[], Any],
        log_drop_check: Callable[..., None],
        forget_inventory_minutes: Callable[[Any], None],
    ):
        """
        claim_rewards(drop_instance_id) sends the claim and returns the GQL
        response. get_inventory() returns the Drops inventory reply.
        drop_analytics() returns the DropAnalytics writer.
        log_drop_check(message, level) writes a [drops-check] line.
        forget_inventory_minutes(drop) drops the inventory minutes of a drop
        whose claim was accepted.
        """
        self._claim_rewards = claim_rewards
        self._get_inventory = get_inventory
        self._drop_analytics = drop_analytics
        self._log_drop_check = log_drop_check
        self._forget_inventory_minutes = forget_inventory_minutes
        # Serializes claims: the sync and prompt-claim passes can reach the
        # same drop at once.
        self.claim_lock = Lock()
        # Drop IDs whose claim Twitch accepted. Kept across inventory syncs,
        # which may never confirm the claim (see Twitch.__finished_drop_ids).
        self.accepted_drop_ids: frozenset[str] = frozenset()
        # drop_instance_id -> monotonic time Twitch accepted its claim, or of
        # the last retry since then.
        self.accepted_drop_claims: dict[str, float] = {}
        # drop_instance_id -> the claim Twitch accepted but will not deliver
        # until the campaign's game account is linked (see
        # _record_unlinked_claim). Written only under the claim lock and
        # always replaced wholesale, so unlinked_drop_report() reads it
        # without the lock.
        self.unlinked_drop_claims: dict[str, dict] = {}
        # Claim requests actually sent, so claim passes only pace requests.
        self.claim_requests_sent = 0
        self.prompt_claim_lock = Lock()
        # Held by every inventory claim pass, so the periodic pass, a prompt
        # claim and a PubSub claim never run at once.
        self.prompt_claim_pass_lock = Lock()
        self.prompt_claim_last: dict[str, float] = {}

    def mark_drop_accepted(self, drop):
        # Twitch accepted the claim (even one it will not deliver until the
        # game account is linked), so the drop has nothing left to watch
        # while the inventory keeps listing it unclaimed. Called under the
        # claim lock; inventory syncs never write this.
        if drop.id:
            self.accepted_drop_ids = self.accepted_drop_ids | {str(drop.id)}
        self._forget_inventory_minutes(drop)

    @staticmethod
    def campaign_link_from_inventory(campaign):
        # The game-account link state of an inventory dropCampaignsInProgress
        # entry, for claim_drop's campaign_link. "connected" is None when
        # Twitch sent no self edge. The campaign's name and game are not
        # carried: the call sites already pass them as overrides.
        if not isinstance(campaign, dict):
            return None
        self_edge = campaign.get("self")
        connected = (
            self_edge.get("isAccountConnected") if isinstance(self_edge, dict) else None
        )
        return {
            "campaign_id": campaign.get("id"),
            "connected": connected if isinstance(connected, bool) else None,
            "url": campaign.get("accountLinkURL") or None,
        }

    @staticmethod
    def _link_campaign_id(campaign, campaign_link):
        if isinstance(campaign_link, dict) and campaign_link.get("campaign_id"):
            return str(campaign_link["campaign_id"])
        campaign_id = getattr(campaign, "id", None)
        return str(campaign_id) if campaign_id else None

    def _unlinked_drop_claims_alive(self, now):
        # Age is the backstop for entries the sync pass never reconciles
        # (the inventory stopped loading, say). "seen" is the last sync that
        # listed the drop unclaimed; the retry stamp alone would age out an
        # entry whose claim is skipped for 12h.
        entries = self.unlinked_drop_claims
        if not entries:
            return {}
        return {
            instance_id: entry
            for instance_id, entry in entries.items()
            if (now - max(entry["stamp"], entry.get("seen", entry["stamp"])))
            < 2 * UNLINKED_CLAIM_RETRY_SECONDS
        }

    def reconcile_unlinked_claims(self, campaigns_in_progress):
        # Entries are refreshed only when a claim pass visits the drop. A drop
        # the inventory stopped listing as unclaimed (delivered after the
        # account got linked, claimed on Twitch, or its campaign ended) must
        # not sit in the daily report until it ages out. A drop missing for
        # less than the grace period is kept: forgetting it on a transient
        # short or empty inventory would re-send the claim and re-warn on
        # the next full one.
        if not self.unlinked_drop_claims:
            return
        now = time.monotonic()
        pending_instance_ids = set()
        delivered_instance_ids = set()
        # (campaign id, drop id): a claimed listing of the same drop id under
        # another campaign is not this instance's delivery.
        delivered_campaign_drops = set()
        for campaign in campaigns_in_progress:
            if not isinstance(campaign, dict):
                continue
            campaign_id = str(campaign["id"]) if campaign.get("id") else None
            for drop in campaign.get("timeBasedDrops", []) or []:
                drop_self = drop.get("self") if isinstance(drop, dict) else None
                if not isinstance(drop_self, dict):
                    continue
                instance_id = drop_self.get("dropInstanceID")
                if drop_self.get("isClaimed") is True:
                    if instance_id:
                        delivered_instance_ids.add(instance_id)
                    if campaign_id and drop.get("id"):
                        delivered_campaign_drops.add((campaign_id, str(drop["id"])))
                elif instance_id:
                    pending_instance_ids.add(instance_id)
        with self.claim_lock:
            alive = self._unlinked_drop_claims_alive(now)
            self.unlinked_drop_claims = {
                instance_id: (
                    {**entry, "seen": now, "missing_since": None}
                    if instance_id in pending_instance_ids
                    else {
                        **entry,
                        "missing_since": entry.get("missing_since") or now,
                    }
                )
                for instance_id, entry in alive.items()
            }
            for instance_id, entry in list(self.unlinked_drop_claims.items()):
                # A repeatable drop lists its earlier, claimed instance next
                # to the pending one; the pending one is not delivered.
                delivered = instance_id in delivered_instance_ids or (
                    instance_id not in pending_instance_ids
                    and (entry["campaign_id"], entry.get("drop_id"))
                    in delivered_campaign_drops
                )
                if not delivered and (
                    entry["missing_since"] is None
                    or (now - entry["missing_since"])
                    < UNLINKED_CLAIM_MISSING_GRACE_SECONDS
                ):
                    continue
                if not delivered:
                    self._log_drop_check(
                        f"drop {entry.get('drop_id')} of campaign "
                        f"{entry['campaign_id']} left the inventory; forgetting "
                        "its unlinked claim",
                        level=logging.DEBUG,
                    )
                self._forget_unlinked_claim(instance_id, entry, delivered)

    def unlinked_drop_report(self):
        """Return the finished Drops Twitch accepted but will not deliver
        until the campaign's game account is linked, grouped by campaign and
        sorted by game and campaign name."""
        by_campaign: dict[str, dict] = {}
        for instance_id, entry in self._unlinked_drop_claims_alive(
            time.monotonic()
        ).items():
            # Without a campaign id, each drop is its own group rather than
            # all of them merging under one name and URL.
            group = by_campaign.setdefault(
                entry["campaign_id"] or instance_id,
                {
                    "campaign_id": entry["campaign_id"],
                    "campaign": entry["campaign"],
                    "category": entry["category"],
                    "url": entry["url"],
                    # The inventory now reports the account linked; the claim
                    # endpoint has not caught up, so the drop is still waiting.
                    "linked": False,
                    "names": {},
                    "count": 0,
                },
            )
            group["names"][entry["drop"]] = group["names"].get(entry["drop"], 0) + 1
            group["count"] += 1
            if group["url"] is None:
                group["url"] = entry["url"]
            if entry.get("connected") is True:
                group["linked"] = True
        report = []
        for group in by_campaign.values():
            names = group.pop("names")
            group["drops"] = [
                name if times == 1 else f"{name} ×{times}"
                for name, times in sorted(names.items())
            ]
            report.append(group)
        return sorted(
            report,
            key=lambda group: (
                str(group["category"] or ""),
                str(group["campaign"] or ""),
            ),
        )

    def _record_unlinked_claim(
        self,
        drop,
        now,
        campaign=None,
        campaign_link=None,
        campaign_name_override=None,
        category_name_override=None,
    ):
        # Twitch accepted the claim but will not deliver the reward until the
        # campaign's game account is linked, and the inventory keeps listing
        # the drop unclaimed. Not a claim: no Claim log, no DROP_CLAIM, no
        # analytics, and the caller keeps is_claimed False. Warn once per
        # campaign (again after a restart, as a reminder) and back off until
        # the inventory reports the account linked.
        instance_id = drop.drop_instance_id
        self.accepted_drop_claims.pop(instance_id, None)
        campaign_id = self._link_campaign_id(campaign, campaign_link)
        campaign_name = campaign_name_override or getattr(campaign, "name", None)
        category_name = category_name_override
        if category_name is None and campaign is not None:
            game = campaign.game or {}
            category_name = game.get("displayName") or game.get("name")
        url = campaign_link.get("url") if isinstance(campaign_link, dict) else None
        alive = self.unlinked_drop_claims
        already_warned = instance_id in alive or (
            campaign_id is not None
            and any(entry["campaign_id"] == campaign_id for entry in alive.values())
        )
        self.unlinked_drop_claims = {
            **alive,
            instance_id: {
                "stamp": now,
                "campaign_id": campaign_id,
                "drop_id": str(drop.id) if getattr(drop, "id", None) else None,
                "campaign": campaign_name,
                "category": category_name,
                "url": url,
                "drop": str(getattr(drop, "name", None) or "Unknown drop"),
                "connected": (
                    campaign_link.get("connected")
                    if isinstance(campaign_link, dict)
                    else None
                ),
            },
        }
        self.mark_drop_accepted(drop)
        if not already_warned:
            logger.warning(
                f'Can\'t claim Drops from "{campaign_name or "Unknown campaign"}" '
                f"({category_name or 'Unknown game'}): link your game account"
                + (f" at {url}" if url else "")
                + ". Earned Drops stay claimable for a while after linking.",
                extra={"emoji": ":warning:", "event": Events.DROP_LINK_REQUIRED},
            )
        return False

    def _forget_unlinked_claim(self, instance_id, entry, delivered):
        # Called under the claim lock. The reward is delivered, so the
        # account got linked; or the inventory stopped listing the drop.
        self.unlinked_drop_claims = {
            other_id: other
            for other_id, other in self.unlinked_drop_claims.items()
            if other_id != instance_id
        }
        if not delivered:
            return
        campaign_id = entry["campaign_id"]
        if campaign_id is not None and any(
            other["campaign_id"] == campaign_id
            for other in self.unlinked_drop_claims.values()
        ):
            return
        # Says what was observed: the inventory does not tell who claimed it.
        logger.info(
            f'Drops from "{entry["campaign"] or "Unknown campaign"}" have been '
            "claimed; no longer waiting on a game account link",
            extra={"emoji": ":link:", "event": Events.DROP_LINK_REQUIRED},
        )

    def claim_drop(
        self,
        drop,
        campaign=None,
        streamer_username=None,
        campaign_name_override=None,
        category_name_override=None,
        campaign_link=None,
    ):
        # Held across the request so a concurrent pass sees the accepted claim
        # instead of claiming and notifying a second time.
        with self.claim_lock:
            return self._claim_drop(
                drop,
                campaign=campaign,
                streamer_username=streamer_username,
                campaign_name_override=campaign_name_override,
                category_name_override=category_name_override,
                campaign_link=campaign_link,
            )

    def _claim_drop(
        self,
        drop,
        campaign=None,
        streamer_username=None,
        campaign_name_override=None,
        category_name_override=None,
        campaign_link=None,
    ):
        now = time.monotonic()
        self.unlinked_drop_claims = self._unlinked_drop_claims_alive(now)
        instance_id = drop.drop_instance_id
        inventory_connected = (
            campaign_link.get("connected") if isinstance(campaign_link, dict) else None
        )
        unlinked = self.unlinked_drop_claims.get(instance_id)
        if unlinked is not None:
            if inventory_connected is True and unlinked.get("connected") is not True:
                # The inventory just started reporting the account as linked:
                # retry now. Only on that edge, because the sync pass runs
                # every minute and a stale flag must not send a claim a
                # minute. Record the flag before sending, so a retry that
                # still fails waits a full interval.
                self._log_drop_check(
                    f"inventory reports the game account for campaign "
                    f"{unlinked['campaign_id']} linked; retrying claim of drop "
                    f"{drop.id}",
                    level=logging.DEBUG,
                )
                self.unlinked_drop_claims = {
                    **self.unlinked_drop_claims,
                    instance_id: {**unlinked, "stamp": now, "connected": True},
                }
            elif (now - unlinked["stamp"]) < (
                # The inventory says linked but the last claim said not: the
                # claim endpoint can lag the link, so retry like an accepted
                # claim rather than waiting the full unlinked interval.
                ACCEPTED_CLAIM_RETRY_SECONDS
                if inventory_connected is True
                else UNLINKED_CLAIM_RETRY_SECONDS
            ):
                # Repeats on every claim pass while the account stays unlinked.
                self._log_drop_check(
                    f"skipping claim of drop {drop.id}; the game account for "
                    f"campaign {unlinked['campaign_id']} is not linked",
                    level=logging.DEBUG,
                )
                link_url = (
                    campaign_link.get("url")
                    if isinstance(campaign_link, dict)
                    else None
                )
                if link_url and unlinked.get("url") != link_url:
                    # Recorded from a pass without the inventory's link URL:
                    # the daily report should still carry it.
                    self.unlinked_drop_claims = {
                        **self.unlinked_drop_claims,
                        instance_id: {**unlinked, "url": link_url},
                    }
                self.mark_drop_accepted(drop)
                return False
            else:
                # Stamp the retry before sending it, as for accepted claims.
                self.unlinked_drop_claims = {
                    **self.unlinked_drop_claims,
                    instance_id: {**unlinked, "stamp": now},
                }
        accepted_at = self.accepted_drop_claims.get(instance_id)
        if accepted_at is not None:
            if (now - accepted_at) < ACCEPTED_CLAIM_RETRY_SECONDS:
                # Repeats on every claim pass while the inventory lags.
                self._log_drop_check(
                    f"skipping claim of drop {drop.id}; Twitch already accepted "
                    "it and the inventory has not caught up yet",
                    level=logging.DEBUG,
                )
                self.mark_drop_accepted(drop)
                return True
            # Stamp the retry before sending it, so a failed retry also waits
            # a full interval instead of repeating on every pass.
            self.accepted_drop_claims[drop.drop_instance_id] = now
            logger.info(
                f"Retrying claim of {drop}; the inventory still lists it as "
                "unclaimed after Twitch accepted it",
                extra={"emoji": ":package:"},
            )

        self.claim_requests_sent = self.claim_requests_sent + 1
        try:
            response = self._claim_rewards(drop.drop_instance_id)
        except RetryError as error:
            logger.error(f"Unable to claim {drop}: {error}")
            return False
        try:
            if response.errors:
                logger.error(f"Unable to claim {drop}: {response.errors}")
                return False
            # Checked before the accepted statuses: an unlinked response is
            # accepted by Twitch but delivers nothing, and must not re-stamp
            # the accepted claim or count as one. An already-claimed drop is
            # delivered whatever the flag says.
            if (
                response.status == "ELIGIBLE_FOR_ALL"
                and getattr(response, "is_user_account_connected", None) is False
            ):
                return self._record_unlinked_claim(
                    drop,
                    now,
                    campaign=campaign,
                    campaign_link=campaign_link,
                    campaign_name_override=campaign_name_override,
                    category_name_override=category_name_override,
                )
            if response.status in [
                "ELIGIBLE_FOR_ALL",
                "DROP_INSTANCE_ALREADY_CLAIMED",
            ]:
                # Entries the inventory has stopped listing are no longer
                # refreshed by retries, so prune them once they age out.
                self.accepted_drop_claims = {
                    instance_id: stamp
                    for instance_id, stamp in self.accepted_drop_claims.items()
                    if (now - stamp) < 2 * ACCEPTED_CLAIM_RETRY_SECONDS
                }
                self.accepted_drop_claims[drop.drop_instance_id] = now
                self.mark_drop_accepted(drop)
                if accepted_at is not None:
                    return True
                if response.status == "DROP_INSTANCE_ALREADY_CLAIMED":
                    # Claimed earlier, for example before a restart: that
                    # claim already sent the notification.
                    logger.info(
                        f"Drop already claimed: {drop}",
                        extra={"emoji": ":package:"},
                    )
                else:
                    logger.info(
                        f"Claim {drop}",
                        extra={"emoji": ":package:", "event": Events.DROP_CLAIM},
                    )
                if unlinked is not None:
                    self._forget_unlinked_claim(instance_id, unlinked, True)
                if campaign is not None:
                    remaining_drops = [
                        campaign_drop
                        for campaign_drop in campaign.drops
                        if campaign_drop.dt_match is True
                        and campaign_drop.is_claimed is False
                        and campaign_drop.id != drop.id
                    ]
                    if not remaining_drops:
                        self._log_drop_check(
                            f"campaign {campaign.id} final drop {drop.id} claimed; "
                            "waiting for inventory completion confirmation"
                        )
                for variant in self._drop_analytics().variant_entries(drop):
                    self._drop_analytics().save_drop(
                        drop,
                        campaign=campaign,
                        streamer_username=streamer_username,
                        campaign_name_override=campaign_name_override,
                        category_name_override=category_name_override,
                        item_name_override=variant.get("name"),
                        benefit_override=variant.get("benefit"),
                        item_art_url_override=variant.get("item_art_url"),
                    )
                return True
            logger.error(f"Unable to claim {drop}: unexpected status {response.status}")
            return False
        except (AttributeError, TypeError):
            return False

    def claim_all_drops_from_inventory(self):
        inventory = self._get_inventory()
        if inventory not in [None, {}]:
            if inventory.get("dropCampaignsInProgress") not in [None, {}]:
                for campaign in inventory["dropCampaignsInProgress"]:
                    if not isinstance(campaign, dict):
                        continue
                    progress_game = campaign.get("game") or {}
                    category_name_override = (
                        progress_game.get("displayName")
                        or progress_game.get("name")
                        or "Unknown"
                    )
                    campaign_name_override = campaign.get("name")
                    campaign_drops = []
                    for drop_dict in campaign.get("timeBasedDrops") or []:
                        # Twitch can send a null self (or drop); one such
                        # entry must not abort claiming for every campaign.
                        if not isinstance(drop_dict, dict) or not isinstance(
                            drop_dict.get("self"), dict
                        ):
                            continue
                        drop = Drop(drop_dict)
                        drop.update(drop_dict["self"])
                        campaign_drops.append(drop)
                        if drop.is_claimable is True:
                            sent_before = self.claim_requests_sent
                            drop.is_claimed = self.claim_drop(
                                drop,
                                campaign_name_override=campaign_name_override,
                                category_name_override=category_name_override,
                                campaign_link=self.campaign_link_from_inventory(
                                    campaign
                                ),
                            )
                            # Pace requests only; a claim skipped by its
                            # backoff sent nothing.
                            if self.claim_requests_sent != sent_before:
                                time.sleep(random.uniform(5, 10))
                    if campaign_drops and all(
                        drop.is_claimed is True for drop in campaign_drops
                    ):
                        campaign_id = campaign.get("id")
                        if campaign_id:
                            self._log_drop_check(
                                f"campaign {campaign_id} final inventory drop claimed; "
                                "waiting for inventory completion confirmation"
                            )

    def claim_drops_from_event(self, data):
        """Handles a PubSub user-drop-events 'drop-claim' message: a drop just became
        claimable, so sweep the inventory now instead of waiting for the next sync.
        Runs the sweep on a background thread; the websocket thread never blocks."""
        if not isinstance(data, dict):
            return
        logger.debug(f"PubSub drop-claim received: {data}")
        if not data.get("drop_instance_id"):
            return
        Thread(
            target=self._claim_drops_from_event_worker,
            name="Drop claim (PubSub)",
            daemon=True,
        ).start()

    def _claim_drops_from_event_worker(self):
        # Give the inventory a moment to reflect the new dropInstanceID
        time.sleep(random.uniform(2, 5))
        # A running pass may hold an inventory that predates this drop: wait for
        # it to finish, then run our own sweep rather than dropping the event.
        if not self.prompt_claim_pass_lock.acquire(timeout=300):
            logger.warning(
                "Drop claim after PubSub event timed out waiting for a running claim pass"
            )
            return
        try:
            self.claim_all_drops_from_inventory()
        except Exception:
            logger.error(
                "Claim after PubSub drop-claim event failed; the sync cycle will retry",
                exc_info=True,
            )
        finally:
            self.prompt_claim_pass_lock.release()

    def claim_completed_drop_promptly(self, drop, campaign):
        # A drop that just reached 100% needs no more watch time, but waiting
        # for the next sync cycle (up to 30 minutes) delays the claim and keeps
        # the campaign in the "watchable" limbo. Claim in the background,
        # debounced so repeated progress saves cannot spam the claim pass.
        if drop.minutes_required <= 0 or drop.drop_instance_id is None:
            return
        now = time.monotonic()
        with self.prompt_claim_lock:
            last = self.prompt_claim_last.get(drop.drop_instance_id)
            if last is not None and (now - last) < PROMPT_CLAIM_DEBOUNCE_SECONDS:
                return
            self.prompt_claim_last[drop.drop_instance_id] = now
            self.prompt_claim_last = {
                instance_id: stamp
                for instance_id, stamp in self.prompt_claim_last.items()
                if (now - stamp) < PROMPT_CLAIM_DEBOUNCE_SECONDS
            }
        Thread(
            target=self._run_prompt_claim,
            args=(drop, campaign),
            name=f"prompt-claim-{campaign.id}",
            daemon=True,
        ).start()

    def _clear_prompt_claim_debounce(self, drop_instance_id):
        # The debounce entry is written optimistically, before the claim is
        # known to succeed. The call site (claim_completed_drop_promptly's
        # caller) is edge-triggered on drop_progress_last_saved, which
        # saturates on the same tick that triggers it, so this can't be
        # re-entered for the same drop - there is no near-term retry through
        # this path either way. Clearing here just avoids leaving an unusable
        # entry behind when lock contention or an exception meant nothing was
        # actually claimed; the real fallback is still the periodic
        # sync_campaigns claim pass, same as before this debounce existed.
        with self.prompt_claim_lock:
            self.prompt_claim_last.pop(drop_instance_id, None)

    def _run_prompt_claim(self, drop, campaign):
        if not self.prompt_claim_pass_lock.acquire(blocking=False):
            logger.info(
                "Prompt claim skipped; an inventory claim pass is already running"
            )
            self._clear_prompt_claim_debounce(drop.drop_instance_id)
            return
        try:
            logger.info(
                f"Drop '{drop.name}' of campaign '{campaign.name}' reached 100%; "
                "claiming inventory drops promptly"
            )
            self.claim_all_drops_from_inventory()
        except Exception:
            logger.error(
                "Prompt claim after drop capture failed; the sync cycle will retry",
                exc_info=True,
            )
            self._clear_prompt_claim_debounce(drop.drop_instance_id)
        finally:
            self.prompt_claim_pass_lock.release()
