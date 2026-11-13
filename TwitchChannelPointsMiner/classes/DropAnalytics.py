"""Write the dashboard's analytics files and keep the session's drop report (#280).

DropAnalytics holds the persistence that used to live in Twitch: the
now-watching list (now_watching.json), drop progress and claims
(drops_by_category.json) and the in-memory drop report used by the
daily report. Twitch keeps the state (the lock and the dictionaries) and
hands it to a DropAnalytics for each call, so code that still reads that
state from Twitch sees every change.
"""

import json
import os
import time

from TwitchChannelPointsMiner.classes.entities.Drop import Drop, utc_now
from TwitchChannelPointsMiner.classes.Settings import Settings, StreamerSource
from TwitchChannelPointsMiner.data_migration import ANALYTICS_DATA_VERSION


class DropAnalytics(object):
    __slots__ = [
        "mutex",
        "report_state",
        "progress_last_saved",
        "status_last_saved",
        "track_item_art",
    ]

    def __init__(
        self,
        mutex,
        report_state,
        progress_last_saved,
        status_last_saved,
        track_item_art=False,
    ):
        # Guards the analytics files and report_state.
        self.mutex = mutex
        # The latest payload per drop, keyed by report_identity_key().
        self.report_state = report_state
        # Minutes and status last written per snapshot key, so an unchanged
        # drop isn't written again.
        self.progress_last_saved = progress_last_saved
        self.status_last_saved = status_last_saved
        self.track_item_art = track_item_art

    def save_now_watching(
        self, streamers_snapshot, streamers_watching, streamer_source
    ):
        if getattr(Settings, "enable_analytics", False) is not True:
            return

        entries = []
        for index in streamers_watching:
            streamer = streamers_snapshot[index]
            source = streamer_source(index)
            if source == StreamerSource.BADGES:
                reason = "badge"
            elif source in (
                StreamerSource.CATEGORIES,
                StreamerSource.WILDCARD_CATEGORIES,
            ):
                reason = "drops"
            else:
                reason = "points"

            game = streamer.stream.game or {}
            game_name = (game.get("displayName") or game.get("name")) if game else None

            entries.append(
                {
                    "username": streamer.username,
                    "reason": reason,
                    "game": game_name,
                    "channel_points": streamer.channel_points,
                }
            )

        analytics_file = os.path.join(Settings.analytics_path, "now_watching.json")
        temp_file = analytics_file + ".temp"

        with self.mutex:
            with open(temp_file, "w", encoding="utf-8") as temp_handle:
                json.dump(entries, temp_handle, indent=4)
            os.replace(temp_file, analytics_file)

    def save_drop(
        self,
        drop,
        campaign=None,
        streamer_username=None,
        status="captured",
        campaign_name_override=None,
        category_name_override=None,
        status_override=None,
        item_name_override=None,
        benefit_override=None,
        item_art_url_override=None,
    ):
        category_name = category_name_override or "Unknown"
        campaign_name = campaign_name_override
        if campaign is not None:
            campaign_name = campaign_name or campaign.name
            game = campaign.game or {}
            category_name = (
                category_name_override
                or game.get("displayName")
                or game.get("name")
                or "Unknown"
            )

        # Ignore uncategorized drop entries to keep category views clean.
        if category_name == "Unknown":
            return

        payload = {
            "x": round(time.time() * 1000),
            "datetime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
            "category": category_name,
            "campaign": campaign_name,
            "item_name": item_name_override or drop.name,
            "benefit": benefit_override or drop.benefit,
            "streamer": streamer_username,
            "drop_id": drop.id,
            "status": status_override or status,
            "current_minutes_watched": min(
                drop.current_minutes_watched, drop.minutes_required
            ),
            "minutes_required": drop.minutes_required,
            "percentage_progress": min(100, drop.percentage_progress),
            "drop_end_at": (
                drop.end_at.strftime("%Y-%m-%dT%H:%M:%SZ")
                if getattr(drop, "end_at", None) is not None
                else None
            ),
            "failed_to_achieve": (
                status != "captured"
                and getattr(drop, "end_at", None) is not None
                and utc_now() > drop.end_at
                and drop.current_minutes_watched < drop.minutes_required
            ),
            "item_art_url": (
                item_art_url_override
                if item_art_url_override is not None and self.track_item_art is True
                else (drop.item_art_url if self.track_item_art is True else None)
            ),
        }

        report_key = self.report_identity_key(payload)
        with self.mutex:
            self.report_state[report_key] = payload.copy()

        if getattr(Settings, "enable_analytics", False) is not True:
            return

        analytics_file = os.path.join(Settings.analytics_path, "drops_by_category.json")
        temp_file = analytics_file + ".temp"

        with self.mutex:
            with open(temp_file, "w", encoding="utf-8") as temp_handle:
                data = {}
                if os.path.isfile(analytics_file):
                    with open(analytics_file, "r", encoding="utf-8") as current:
                        try:
                            data = json.load(current)
                        except json.JSONDecodeError:
                            data = {}

                if "drops" not in data:
                    data["drops"] = []
                data["version"] = ANALYTICS_DATA_VERSION

                compacted: dict[str, dict] = {}
                for item in data["drops"]:
                    identity_key = self.report_identity_key(item)
                    previous = compacted.get(identity_key)
                    if previous is None or (item.get("x") or 0) >= (
                        previous.get("x") or 0
                    ):
                        compacted[identity_key] = item

                data["drops"] = list(compacted.values())

                payload_identity_key = self.report_identity_key(payload)
                identity_index = next(
                    (
                        i
                        for i, item in enumerate(data["drops"])
                        if self.report_identity_key(item) == payload_identity_key
                    ),
                    None,
                )

                if identity_index is None:
                    data["drops"].append(payload)
                else:
                    data["drops"][identity_index] = payload

                json.dump(data, temp_handle, indent=4)

            os.replace(temp_file, analytics_file)

    def save_drop_progress(
        self,
        drop,
        campaign=None,
        streamer_username=None,
        campaign_name_override=None,
        category_name_override=None,
        status_override=None,
        item_name_override=None,
        benefit_override=None,
        item_art_url_override=None,
    ):
        self.save_drop(
            drop,
            campaign=campaign,
            streamer_username=streamer_username,
            status="in_progress",
            campaign_name_override=campaign_name_override,
            category_name_override=category_name_override,
            status_override=status_override,
            item_name_override=item_name_override,
            benefit_override=benefit_override,
            item_art_url_override=item_art_url_override,
        )

    @staticmethod
    def report_identity_key(entry):
        # Mirrors the dedup key in assets/script.js (dashboard rendering): the
        # same drop_id can be recorded under different item_name values across
        # its lifecycle (a friendly display name while actively watched vs. a
        # shorter internal name from the periodic full-inventory sync), so a
        # composite key including item_name let a completed drop's "captured"
        # snapshot land under a different key than its earlier "in_progress"
        # snapshot - leaving a stale ghost row that never collapsed. Key on
        # drop_id alone when available; fall back to the old composite key
        # only when drop_id itself is missing.
        drop_id = str(entry.get("drop_id") or "")
        category = str(entry.get("category") or "") or "unknown"
        if drop_id:
            return "|".join(["id", drop_id, category])
        return "|".join(
            [
                "",
                str(entry.get("item_art_url") or ""),
                str(entry.get("item_name") or "") or "unknown",
                str(entry.get("campaign") or "") or "unknown",
                category,
            ]
        )

    def report_snapshot(self):
        """Return the latest structured drop state tracked by this session."""
        with self.mutex:
            return {
                tracking_key: payload.copy()
                for tracking_key, payload in self.report_state.items()
            }

    def restore_report_state(self, snapshot):
        """Seed the drop state from a previous session without overriding newer entries."""
        if not isinstance(snapshot, dict):
            return
        with self.mutex:
            for tracking_key, payload in snapshot.items():
                if isinstance(payload, dict):
                    self.report_state.setdefault(tracking_key, payload.copy())

    @staticmethod
    def captured_history():
        if getattr(Settings, "enable_analytics", False) is not True:
            return []

        analytics_file = os.path.join(Settings.analytics_path, "drops_by_category.json")
        if os.path.isfile(analytics_file) is False:
            return []

        try:
            with open(analytics_file, "r", encoding="utf-8") as current:
                data = json.load(current)
        except (OSError, json.JSONDecodeError):
            return []

        drops = data.get("drops") if isinstance(data, dict) else None
        if not isinstance(drops, list):
            return []
        return [
            item
            for item in drops
            if isinstance(item, dict) and item.get("status") == "captured"
        ]

    def reconcile_awarded_game_event_drops(self, inventory):
        if getattr(Settings, "enable_analytics", False) is not True:
            return 0

        if not isinstance(inventory, dict):
            return 0

        rewards = inventory.get("gameEventDrops") or []
        if not isinstance(rewards, list) or rewards == []:
            return 0

        awarded_by_name: dict[str, set] = {}
        for reward in rewards:
            if not isinstance(reward, dict):
                continue
            if reward.get("lastAwardedAt") in [None, ""]:
                continue

            reward_name = reward.get("name")
            if reward_name in [None, ""]:
                continue

            reward_image = reward.get("imageURL")
            if reward_name not in awarded_by_name:
                awarded_by_name[reward_name] = set()
            if reward_image not in [None, ""]:
                awarded_by_name[reward_name].add(reward_image)

        if awarded_by_name == {}:
            return 0

        analytics_file = os.path.join(Settings.analytics_path, "drops_by_category.json")
        if os.path.isfile(analytics_file) is False:
            return 0

        temp_file = analytics_file + ".temp"
        now_ms = round(time.time() * 1000)
        now_dt = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        updated = 0

        with self.mutex:
            with open(analytics_file, "r", encoding="utf-8") as current:
                try:
                    data = json.load(current)
                except json.JSONDecodeError:
                    data = {}

            drops = data.get("drops")
            if not isinstance(drops, list):
                drops = []

            for item in drops:
                if not isinstance(item, dict):
                    continue
                if item.get("status") == "captured":
                    continue

                item_name = item.get("item_name")
                if item_name not in awarded_by_name:
                    continue

                known_images = awarded_by_name.get(item_name, set())
                item_art_url = item.get("item_art_url")
                if (
                    known_images
                    and item_art_url not in [None, ""]
                    and item_art_url not in known_images
                ):
                    continue

                item["status"] = "captured"
                item["failed_to_achieve"] = False
                item["x"] = now_ms
                item["datetime"] = now_dt

                minutes_required = item.get("minutes_required") or 0
                current_minutes = item.get("current_minutes_watched") or 0
                item["current_minutes_watched"] = max(current_minutes, minutes_required)

                percentage_progress = item.get("percentage_progress") or 0
                item["percentage_progress"] = max(percentage_progress, 100)
                updated += 1

            data["drops"] = drops
            data["version"] = ANALYTICS_DATA_VERSION

            with open(temp_file, "w", encoding="utf-8") as temp_handle:
                json.dump(data, temp_handle, indent=4)

            os.replace(temp_file, analytics_file)

        return updated

    def tracking_key(self, drop, campaign_name=None, category_name=None):
        return "|".join(
            [
                str(drop.id or ""),
                str(drop.item_art_url or ""),
                str(drop.name or ""),
                str(campaign_name or ""),
                str(category_name or ""),
            ]
        )

    def should_save_snapshot(self, tracking_key, minutes_watched, status):
        last_saved_minutes = self.progress_last_saved.get(tracking_key, -1)
        last_saved_status = self.status_last_saved.get(tracking_key)
        if minutes_watched > last_saved_minutes:
            return True

        # Terminal state updates can happen without minute changes.
        if status in ["captured", "failed"] and status != last_saved_status:
            return True

        return False

    def mark_snapshot_saved(self, tracking_key, minutes_watched, status):
        self.progress_last_saved[tracking_key] = minutes_watched
        self.status_last_saved[tracking_key] = status

    def save_snapshot_from_dict(
        self,
        drop_dict,
        campaign=None,
        streamer_username=None,
        campaign_name_override=None,
        category_name_override=None,
        status_override=None,
    ):
        drop_self = drop_dict.get("self")
        if not isinstance(drop_self, dict):
            return

        try:
            drop = Drop(drop_dict)
            drop.update(drop_self)
        except (KeyError, TypeError, ValueError):
            return

        if drop.has_preconditions_met is False:
            return

        is_done = (
            drop.is_claimed is True
            or drop.current_minutes_watched >= drop.minutes_required
        )
        is_expired = (
            getattr(drop, "end_at", None) is not None and utc_now() > drop.end_at
        )

        progress_key = self.tracking_key(
            drop,
            campaign_name=campaign_name_override,
            category_name=category_name_override,
        )
        status = (
            "captured"
            if is_done is True
            else (
                "failed"
                if is_expired and drop.current_minutes_watched < drop.minutes_required
                else "in_progress"
            )
        )

        effective_status = status_override or status

        for variant in self.variant_entries(drop):
            variant_key = "|".join(
                [
                    progress_key,
                    str(variant.get("name") or ""),
                    str(variant.get("item_art_url") or ""),
                ]
            )
            if (
                self.should_save_snapshot(
                    variant_key,
                    drop.current_minutes_watched,
                    effective_status,
                )
                is False
            ):
                continue

            self.mark_snapshot_saved(
                variant_key,
                drop.current_minutes_watched,
                effective_status,
            )
            self.save_drop_progress(
                drop,
                campaign=campaign,
                streamer_username=streamer_username,
                campaign_name_override=campaign_name_override,
                category_name_override=category_name_override,
                status_override=effective_status,
                item_name_override=variant.get("name"),
                benefit_override=variant.get("benefit"),
                item_art_url_override=variant.get("item_art_url"),
            )

    def variant_entries(self, drop):
        variants = []
        benefit_edges = getattr(drop, "benefit_edges", []) or []

        if benefit_edges == []:
            variants.append(
                {
                    "name": drop.name,
                    "benefit": drop.benefit,
                    "item_art_url": drop.item_art_url,
                }
            )
            return variants

        for edge in benefit_edges:
            benefit = edge.get("benefit", {}) if isinstance(edge, dict) else {}
            variants.append(
                {
                    "name": benefit.get("name") or drop.name,
                    "benefit": benefit.get("name") or drop.benefit,
                    "item_art_url": self.edge_art_url(benefit) or drop.item_art_url,
                }
            )

        return variants

    def edge_art_url(self, benefit):
        if not isinstance(benefit, dict):
            return None

        for key in [
            "imageAssetURL",
            "imageAssetUrl",
            "imageURL",
            "imageUrl",
            "thumbnailURL",
            "thumbnailUrl",
        ]:
            if benefit.get(key):
                return benefit.get(key)
        return None
