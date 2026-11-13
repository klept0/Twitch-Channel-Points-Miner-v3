"""Choose which online streamers to watch each minute (#280).

StreamSelector holds the selection policy that used to live inline in
Twitch.send_minute_watched_events(): source and priority ordering, the
Drops slot arbitration between discovered and configured streams, and
the stickiness of the previous Drops pick. It reads a snapshot of the
streamers and of the Drops state it is given, and returns the indexes
to watch with what the caller needs to log and remember. It sends no
requests and changes no shared state; Twitch passes in its own checks
(``drops_condition`` and the others) and applies the result.
"""

import functools
from dataclasses import dataclass
from datetime import datetime, timedelta
from collections.abc import Collection, Sequence
from typing import Any, Callable, Optional

from TwitchChannelPointsMiner.classes.entities.Drop import utc_now
from TwitchChannelPointsMiner.classes.entities.Streamer import (
    discovery_source,
    is_drop_discovered,
)
from TwitchChannelPointsMiner.classes.Settings import Priority, StreamerSource


@dataclass(frozen=True, slots=True)
class DropTierPick(object):
    """The Drops pick of one discovered tier (categories or wildcards)."""

    # The tier's pick when it is actually watched this minute, else None.
    watched_pick: Optional[int]
    # The tier's online candidates, soonest-expiring first (sticky pick
    # fronted).
    ordered: list[int]
    # Why the previous pick kept the slot, when stickiness or an
    # in-progress drop decided it.
    reason: Optional[str]
    # The tier's candidates that won a watch slot before arbitration.
    slotted_candidates: list[int]
    # The configured or followed streamer that earns the same campaigns,
    # when it replaced this tier's pick.
    covered_by: Optional[str]


@dataclass(frozen=True, slots=True)
class StreamSelection(object):
    """What StreamSelector.select() decided for one minute."""

    # Indexes into the streamers list, in watch order.
    watching: list[int]
    streamer_source: Callable[[int], StreamerSource]
    is_drop_stream: Callable[[int], bool]
    category_expiration: Callable[[int], datetime]
    category: DropTierPick
    wildcard: DropTierPick
    # Remembered as Twitch.last_drop_pick_streamer for stickiness.
    drop_pick_streamer: Optional[str]
    # Remembered as Twitch.drop_slot_covering_streamer.
    drop_slot_covering_streamer: Optional[str]
    # Identifies the Drops slot conflict, so a repeat is logged once.
    drop_conflict_key: Optional[tuple]
    drop_conflict_message: Optional[str]


class StreamSelector(object):
    __slots__ = [
        "_drops_condition",
        "_previous_pick_still_farming",
        "_has_reached_points_limit",
        "_has_pending_watch_streak",
        "_in_progress_drop_needs",
        "_slugify",
        "_previous_drop_pick",
        "_category_deadlines",
        "_category_drops_override_streamers",
        "_configured_category_slugs",
        "_drop_cover_revoked",
        "_drop_inventory_progress",
    ]

    def __init__(
        self,
        *,
        drops_condition: Callable[[Any], Any],
        previous_pick_still_farming: Callable[[Any], Any],
        has_reached_points_limit: Callable[[Any], Any],
        has_pending_watch_streak: Callable[[Any, float], Any],
        in_progress_drop_needs: Callable[[str, str], Optional[tuple]],
        slugify: Callable[[str], str],
        previous_drop_pick: Optional[str] = None,
        category_deadlines: Optional[dict] = None,
        category_drops_override_streamers: bool = False,
        configured_category_slugs: Optional[set] = None,
        drop_cover_revoked: Optional[set] = None,
        drop_inventory_progress: Optional[dict] = None,
    ):
        self._drops_condition = drops_condition
        self._previous_pick_still_farming = previous_pick_still_farming
        self._has_reached_points_limit = has_reached_points_limit
        self._has_pending_watch_streak = has_pending_watch_streak
        self._in_progress_drop_needs = in_progress_drop_needs
        self._slugify = slugify
        # The streamer picked for the Drops slot last minute.
        self._previous_drop_pick = previous_drop_pick
        # Soonest campaign deadline per category slug.
        self._category_deadlines = (
            {} if category_deadlines is None else category_deadlines
        )
        self._category_drops_override_streamers = category_drops_override_streamers
        self._configured_category_slugs = configured_category_slugs
        # (game slug, username, broadcast id) of covering streams that
        # Twitch did not credit.
        self._drop_cover_revoked = drop_cover_revoked
        # Inventory progress by game slug.
        self._drop_inventory_progress = drop_inventory_progress

    def select(
        self,
        streamers_snapshot: Sequence[Any],
        now: float,
        priority: Sequence[Priority],
        streams_watched: int,
        source_priority: Optional[Sequence[StreamerSource]] = None,
        drop_progress_stall_minutes: float = 10,
        drop_pick_stickiness_minutes: float = 0,
        drop_progress_cooldowns: Collection[tuple[str, str]] = frozenset(),
    ) -> StreamSelection:
        """Pick the streamers to watch this minute.

        ``streamers_snapshot`` is a list the caller won't change while this
        runs. ``drop_progress_cooldowns`` holds the (game slug, username)
        pairs whose Drops progress stalled. Returns indexes into
        ``streamers_snapshot``.
        """
        streamers_index = [
            i
            for i in range(0, len(streamers_snapshot))
            if streamers_snapshot[i].is_online is True
            and (
                not is_drop_discovered(streamers_snapshot[i])
                or self._drops_condition(streamers_snapshot[i]) is True
                # A transient per-channel eligibility failure must not
                # exclude the previous drop pick before the rescue
                # check below (Priority.DROPS) ever gets to see it -
                # without this, __previous_pick_still_farming's
                # streamer was already filtered out here and the
                # rescue path could never run.
                or self._previous_pick_still_farming(streamers_snapshot[i]) is True
            )
            and (
                streamers_snapshot[i].online_at == 0
                or (now - streamers_snapshot[i].online_at) > 30
            )
            and (
                self._has_reached_points_limit(streamers_snapshot[i]) is False
                or self._has_pending_watch_streak(streamers_snapshot[i], now)
            )
            and (
                (
                    self._slugify(streamers_snapshot[i].stream.game_name() or ""),
                    streamers_snapshot[i].username,
                )
                not in drop_progress_cooldowns
            )
        ]

        """
        Twitch has a limit - you can't watch more than 2 channels at one time.
        Take the configured number of streamers from the final list in priority order.
        """
        # Twitch counts watch time on up to two streams, but Drops progress
        # applies to only one. The category safety net below reserves at
        # most one selection for a discovered Drops stream.
        max_watch_amount = streams_watched
        # Indexes given a watch slot, before the Drops arbitration below.
        slotted: set[int] = set()

        default_source_priority = [
            StreamerSource.STREAMERS,
            StreamerSource.FOLLOWERS,
            StreamerSource.CATEGORIES,
            StreamerSource.BADGES,
            StreamerSource.WILDCARD_CATEGORIES,
        ]
        normalized_source_priority = []
        for source in source_priority or default_source_priority:
            if (
                isinstance(source, StreamerSource)
                and source not in normalized_source_priority
            ):
                normalized_source_priority.append(source)
        for source in default_source_priority:
            if source not in normalized_source_priority:
                normalized_source_priority.append(source)
        source_priority = normalized_source_priority

        def streamer_source(index):
            return discovery_source(streamers_snapshot[index], source_priority)

        def remaining_watch_amount():
            return max_watch_amount - len(slotted)

        category_deadlines = self._category_deadlines

        def category_expiration(index):
            game_slug = self._slugify(
                streamers_snapshot[index].stream.game_name() or ""
            )
            return category_deadlines.get(game_slug, datetime.max)

        indexes_by_source = {
            source: [
                index for index in streamers_index if streamer_source(index) == source
            ]
            for source in source_priority
        }
        # Category-discovered streams don't get a slot each in priority
        # order like the other sources do (only one is ever watched at
        # a time, below) - order them so whichever campaign is closest
        # to expiring is always the one considered/selected first.
        indexes_by_source[StreamerSource.CATEGORIES].sort(key=category_expiration)
        # Wildcard-discovered streams get the same soonest-expiring-first
        # ordering as preferred categories, but as an independent tier --
        # see the safety net below, which caps each tier at one watch
        # slot per cycle separately so neither can crowd out the other.
        indexes_by_source[StreamerSource.WILDCARD_CATEGORIES].sort(
            key=category_expiration
        )

        # Stickiness: a bare expiration sort hands the tier's watch
        # slot to whichever campaign happens to be closest to expiring
        # this cycle, so the watched campaign churns every time
        # deadlines re-rank by a minute or the discovered channel set
        # changes. When the previously picked streamer is still
        # eligible and the best alternative is not more than the
        # stickiness margin closer to expiring, move the previous pick
        # to the front of the tier so it keeps the slot (0 disables
        # the behavior).
        stickiness_margin = timedelta(
            minutes=max(float(drop_pick_stickiness_minutes), 0)
        )
        previous_drop_pick = self._previous_drop_pick

        def _sticky_order(indexes):
            # Returns (indexes, sticky_held): sticky_held is True when
            # the previous pick was moved to the front by the margin
            # rule, so the final pick log can attribute the choice to
            # stickiness instead of plain expiration order. Works on a
            # copy: the caller's tier list must stay untouched.
            indexes = list(indexes)
            if (
                not indexes
                or stickiness_margin <= timedelta(0)
                or previous_drop_pick is None
            ):
                return indexes, False
            previous_index = next(
                (
                    index
                    for index in indexes
                    if streamers_snapshot[index].username == previous_drop_pick
                ),
                None,
            )
            if previous_index is None:
                return indexes, False
            best_index = min(indexes, key=category_expiration)
            if best_index == previous_index:
                return indexes, False
            best_deadline = category_expiration(best_index)
            previous_deadline = category_expiration(previous_index)
            if (
                best_deadline < previous_deadline
                and (previous_deadline - best_deadline) > stickiness_margin
            ):
                # The alternative expires sooner by more than the
                # margin - let the expiration sort win.
                return indexes, False
            # The previous pick is at least as urgent (this also
            # covers both deadlines being unknown) - keep it.
            indexes.remove(previous_index)
            indexes.insert(0, previous_index)
            return indexes, True

        def _stickiness_hold_reason(previous_index, best_index):
            return (
                "held by stickiness (margin "
                f"{stickiness_margin.total_seconds() / 60:.0f}m): "
                f"{streamers_snapshot[previous_index].username} kept "
                f"({_deadline_label(category_expiration(previous_index))} "
                "vs "
                f"{_deadline_label(category_expiration(best_index))} for "
                f"{streamers_snapshot[best_index].username})"
            )

        def _deadline_label(deadline):
            if deadline == datetime.max:
                return "no known deadline"
            minutes = max((deadline - utc_now()).total_seconds() / 60, 0)
            return f"{minutes:.0f}m left"

        (
            indexes_by_source[StreamerSource.CATEGORIES],
            category_sticky_held,
        ) = _sticky_order(indexes_by_source[StreamerSource.CATEGORIES])
        (
            indexes_by_source[StreamerSource.WILDCARD_CATEGORIES],
            wildcard_sticky_held,
        ) = _sticky_order(indexes_by_source[StreamerSource.WILDCARD_CATEGORIES])

        def _hold_reason(index, deadline, challenger_deadline):
            # Why the previously picked streamer must keep the slot:
            # its game still has an in-progress drop that can finish
            # before the campaign deadline. Returns None when there is
            # no such drop (or it can no longer complete in time).
            stream = getattr(streamers_snapshot[index], "stream", None)
            if stream is None:
                return None
            needs = self._in_progress_drop_needs(
                self._slugify(stream.game_name() or ""),
                streamers_snapshot[index].username,
            )
            if needs is None:
                return None
            needs_minutes, drop_name = needs
            if deadline == datetime.max:
                if challenger_deadline != datetime.max:
                    # A real, known deadline on the challenger should
                    # not lose to a hold with nothing to actually
                    # measure feasibility against.
                    return None
                return (
                    f"holding in-progress drop '{drop_name}' "
                    f"(needs {needs_minutes:.0f}m, no campaign deadline)"
                )
            minutes_to_deadline = max((deadline - utc_now()).total_seconds() / 60, 0)
            if needs_minutes > minutes_to_deadline:
                return None
            return (
                f"holding in-progress drop '{drop_name}' "
                f"(needs {needs_minutes:.0f}m, campaign deadline in "
                f"{minutes_to_deadline:.0f}m)"
            )

        def _prefer_sticky(candidates, best_index):
            # Same margin rule as _sticky_order, applied to the final
            # per-tier choice once the watch slots are allocated.
            if (
                not candidates
                or best_index is None
                or stickiness_margin <= timedelta(0)
            ):
                return best_index, None
            previous_index = next(
                (
                    index
                    for index in candidates
                    if streamers_snapshot[index].username == previous_drop_pick
                ),
                None,
            )
            if previous_index is None or previous_index == best_index:
                return best_index, None
            best_deadline = category_expiration(best_index)
            previous_deadline = category_expiration(previous_index)
            if best_deadline >= previous_deadline:
                # The previous pick is at least as urgent (this also
                # covers both deadlines being unknown) - keep it.
                return previous_index, None
            if (previous_deadline - best_deadline) > stickiness_margin:
                # Normally the more urgent campaign wins - but a drop
                # that is still being farmed and can finish before its
                # deadline is worth more than switching to another
                # campaign and restarting its progress from zero.
                hold_reason = _hold_reason(
                    previous_index, previous_deadline, best_deadline
                )
                if hold_reason is not None:
                    return previous_index, hold_reason
                return best_index, None
            # The previous pick stays within the stickiness margin, so
            # it keeps the slot over the more urgent challenger - say
            # so, instead of falling back to the soonest-expiring text.
            return (
                previous_index,
                _stickiness_hold_reason(previous_index, best_index),
            )

        for prior in priority:
            if remaining_watch_amount() <= 0:
                break

            for source in source_priority:
                if remaining_watch_amount() <= 0:
                    break

                available_source_indexes = [
                    index for index in indexes_by_source[source] if index not in slotted
                ]

                if prior == Priority.ORDER:
                    slotted.update(available_source_indexes[: remaining_watch_amount()])

                elif prior == Priority.FAVORITE:
                    favorite_indexes = [
                        index
                        for index in available_source_indexes
                        if streamers_snapshot[index].settings.favorite
                    ]
                    slotted.update(favorite_indexes[: remaining_watch_amount()])

                elif prior in [
                    Priority.POINTS_ASCENDING,
                    Priority.POINTS_DESCENDING,
                ]:
                    items = [
                        {
                            "points": streamers_snapshot[index].channel_points,
                            "index": index,
                        }
                        for index in available_source_indexes
                    ]
                    items = sorted(
                        items,
                        key=lambda x: x["points"],
                        reverse=(prior == Priority.POINTS_DESCENDING),
                    )
                    slotted.update(
                        [item["index"] for item in items][: remaining_watch_amount()]
                    )

                elif prior == Priority.STREAK:
                    for index in available_source_indexes:
                        if self._has_pending_watch_streak(
                            streamers_snapshot[index], now
                        ):
                            slotted.add(index)
                            if remaining_watch_amount() <= 0:
                                break

                elif prior == Priority.DROPS:
                    for index in available_source_indexes:
                        streamer = streamers_snapshot[index]
                        if (
                            self._drops_condition(streamer) is True
                            or self._previous_pick_still_farming(streamer) is True
                        ):
                            slotted.add(index)
                            if remaining_watch_amount() <= 0:
                                break

                elif prior == Priority.SUBSCRIBED:
                    streamers_with_multiplier = [
                        index
                        for index in available_source_indexes
                        if streamers_snapshot[index].viewer_has_points_multiplier()
                    ]
                    streamers_with_multiplier = sorted(
                        streamers_with_multiplier,
                        key=lambda x: streamers_snapshot[x].total_points_multiplier(),
                        reverse=True,
                    )
                    slotted.update(
                        streamers_with_multiplier[: remaining_watch_amount()]
                    )

        source_rank = {source: rank for rank, source in enumerate(source_priority)}
        streamers_watching = sorted(
            slotted,
            key=lambda index: (
                source_rank[streamer_source(index)],
                streamers_index.index(index),
            ),
        )[:max_watch_amount]

        # Tier membership (not the from_category/from_wildcard_category
        # attributes alone) decides who can win the shared discovered
        # slot below and who the later trim loop evicts - a
        # BADGES-source streamer can also carry from_category=True, so
        # an attribute-only check here let a badge stream win the slot
        # and then evade the trim loop (which already used tier
        # membership), silently dropping a genuine category/wildcard
        # candidate for the whole cycle.
        preferred_tier = set(indexes_by_source[StreamerSource.CATEGORIES])
        wildcard_tier = set(indexes_by_source[StreamerSource.WILDCARD_CATEGORIES])

        # Safety net: never watch more than one discovered ("not
        # explicitly chosen by the user") Drops stream per cycle in
        # total, across the preferred-category and wildcard-category
        # tiers combined. Twitch only accrues Drops progress on one of
        # the (up to two) watched streams regardless of source, so
        # letting each tier keep its own slot would waste a slot that
        # could otherwise go to an explicit/follower stream. When
        # candidates exist in both tiers this cycle, the shared slot
        # goes to whichever tier the user ranks higher in
        # source_priority (see kept_discovered_index below) - only
        # when just one tier has a candidate does that one get it
        # unconditionally.

        # Final choice per tier: min() by expiration, but keep the
        # previously picked streamer while it is still within the
        # stickiness margin (the tier ordering above already fronted
        # it for slot allocation).
        category_candidates = [
            index for index in streamers_watching if index in preferred_tier
        ]
        best_category_index = (
            min(category_candidates, key=category_expiration)
            if category_candidates
            else None
        )
        best_category_index, category_hold_reason = _prefer_sticky(
            category_candidates, best_category_index
        )
        if (
            category_hold_reason is None
            and category_sticky_held
            and best_category_index is not None
            and previous_drop_pick is not None
            and streamers_snapshot[best_category_index].username == previous_drop_pick
        ):
            # The tier ordering fronted the previous pick (within the
            # stickiness margin) and it won the slot over the more
            # urgent tier minimum - attribute the choice correctly.
            tier_min_index = min(
                indexes_by_source[StreamerSource.CATEGORIES],
                key=category_expiration,
            )
            if best_category_index != tier_min_index:
                category_hold_reason = _stickiness_hold_reason(
                    best_category_index, tier_min_index
                )
        wildcard_category_candidates = [
            index for index in streamers_watching if index in wildcard_tier
        ]
        best_wildcard_category_index = (
            min(wildcard_category_candidates, key=category_expiration)
            if wildcard_category_candidates
            else None
        )
        best_wildcard_category_index, wildcard_hold_reason = _prefer_sticky(
            wildcard_category_candidates, best_wildcard_category_index
        )
        if (
            wildcard_hold_reason is None
            and wildcard_sticky_held
            and best_wildcard_category_index is not None
            and previous_drop_pick is not None
            and streamers_snapshot[best_wildcard_category_index].username
            == previous_drop_pick
        ):
            tier_min_index = min(
                indexes_by_source[StreamerSource.WILDCARD_CATEGORIES],
                key=category_expiration,
            )
            if best_wildcard_category_index != tier_min_index:
                wildcard_hold_reason = _stickiness_hold_reason(
                    best_wildcard_category_index, tier_min_index
                )
        # The shared slot goes to whichever tier the user ranks higher
        # in streamer_source_priority (CATEGORIES before
        # WILDCARD_CATEGORIES by default) - not a hardcoded
        # preference for categories, so a user who has deliberately
        # reordered wildcard categories ahead of preferred ones gets
        # that honored here too.
        if best_category_index is None:
            kept_discovered_index = best_wildcard_category_index
        elif best_wildcard_category_index is None:
            kept_discovered_index = best_category_index
        elif (
            source_rank[StreamerSource.WILDCARD_CATEGORIES]
            < source_rank[StreamerSource.CATEGORIES]
        ):
            kept_discovered_index = best_wildcard_category_index
        else:
            kept_discovered_index = best_category_index

        # Badge campaigns are time-based Drops campaigns too, and
        # Twitch credits Drops progress to only one watched channel at
        # a time - watching a badge stream next to the discovered
        # category/wildcard Drops stream splits progress so neither
        # campaign reliably advances. Keep whichever the user ranks
        # higher in source_priority; the freed slot is backfilled with
        # a non-discovered points stream below.
        badge_tier = set(indexes_by_source[StreamerSource.BADGES])
        kept_badge_index = next(
            (index for index in streamers_watching if index in badge_tier),
            None,
        )
        if kept_badge_index is not None and kept_discovered_index is not None:
            if (
                source_rank[StreamerSource.BADGES]
                < source_rank[streamer_source(kept_discovered_index)]
            ):
                kept_discovered_index = None
            else:
                kept_badge_index = None

        # Category and badge discovery keep this channel in their
        # results while it is live in its game, so a refresh cannot
        # take the slot away from it (see __keep_drop_pick).
        kept_pick_index = (
            kept_discovered_index
            if kept_discovered_index is not None
            else kept_badge_index
        )
        drop_pick_streamer = (
            streamers_snapshot[kept_pick_index].username
            if kept_pick_index is not None
            else None
        )

        # Only discovered category/wildcard tier members are trimmed
        # here - membership in the tier lists (preferred_tier /
        # wildcard_tier, defined above), not the from_category flag
        # alone, since a BADGES-source streamer can also carry
        # from_category=True.
        # Badge-campaign streamers are capped at one watched stream
        # (kept_badge_index, chosen above against the discovered Drops
        # pick): badge discovery keeps adding a fresh live channel for
        # the same campaign each refresh, so without a cap two channels
        # for one badge campaign could fill both watch slots.
        filtered_streamers_watching = []
        for index in streamers_watching:
            if (
                index in preferred_tier or index in wildcard_tier
            ) and index != kept_discovered_index:
                continue
            if index in badge_tier and index != kept_badge_index:
                continue
            filtered_streamers_watching.append(index)

        # Twitch credits Drops progress to only one watched channel at
        # a time, whatever the source - a configured or followed
        # streamer playing a Drops game competes with the discovered
        # Drops pick just like a badge stream does. Keep the single
        # Drops stream the user ranks highest in source_priority and
        # evict the rest; their slots are backfilled with points-only
        # streams below.
        discovered_tiers = preferred_tier | wildcard_tier | badge_tier

        # The helpers below are memoized per cycle: the same indexes
        # are asked about repeatedly while the slots are settled.
        @functools.lru_cache(maxsize=None)
        def _is_drop_stream(index):
            return (
                index in discovered_tiers
                or self._drops_condition(streamers_snapshot[index]) is True
            )

        override_streamer_drops = self._category_drops_override_streamers is True
        configured_category_slugs = self._configured_category_slugs or set()

        def _streamer_drops_demoted(index):
            # category_drops_override_streamers: a configured or
            # followed streamer's Drops for a game outside the
            # configured category list rank below every discovered
            # Drops source.
            return (
                override_streamer_drops
                and streamer_source(index)
                in (StreamerSource.STREAMERS, StreamerSource.FOLLOWERS)
                and self._slugify(streamers_snapshot[index].stream.game_name() or "")
                not in configured_category_slugs
            )

        def _drop_rank(index):
            rank = source_rank[streamer_source(index)]
            if _streamer_drops_demoted(index):
                rank += len(source_rank)
            return rank

        def _watchable_campaigns(index):
            return [
                campaign
                for campaign in getattr(
                    streamers_snapshot[index].stream, "campaigns", None
                )
                or []
                if campaign.has_watchable_drops()
            ]

        @functools.lru_cache(maxsize=None)
        def _drop_campaign_key(index):
            # The campaign IDs a stream would earn, or None when that
            # is unknown or any campaign is limited to listed
            # channels (sharing the game is not enough: per-channel
            # campaigns of one game are still separate campaigns).
            campaigns = _watchable_campaigns(index)
            if not campaigns or any(
                getattr(campaign, "channels", None) for campaign in campaigns
            ):
                return None
            return frozenset(campaign.id for campaign in campaigns)

        drop_cover_revoked = self._drop_cover_revoked or set()
        # The stall check is a covering stream's safety net: it needs
        # stall detection on and the game's progress in the
        # inventory, or an uncredited stream would hold the slot
        # unnoticed.
        cover_games = (
            set(self._drop_inventory_progress or {})
            if float(drop_progress_stall_minutes) > 0
            else set()
        )

        @functools.lru_cache(maxsize=None)
        def _channel_earned_campaign_ids(index):
            # The campaigns a stream provably earns: ones Twitch's
            # channel query advertised for this broadcast (game-level
            # fallback IDs do not prove Drops are enabled on it).
            # Empty once Twitch failed to credit the stream while it
            # stood in for a discovered pick, or while the stall
            # check could not watch it.
            streamer = streamers_snapshot[index]
            game_slug = self._slugify(streamer.stream.game_name() or "")
            if (
                game_slug not in cover_games
                or (
                    game_slug,
                    streamer.username,
                    getattr(streamer.stream, "broadcast_id", None),
                )
                in drop_cover_revoked
            ):
                return frozenset()
            advertised = set(
                getattr(streamer.stream, "channel_campaign_ids", None) or []
            )
            return frozenset(
                campaign.id
                for campaign in _watchable_campaigns(index)
                if campaign.id in advertised
            )

        def _covers_discovered(index, discovered_index):
            # A discovered (category/wildcard/badge) stream is only
            # watched to earn its campaigns. When a configured or
            # followed stream - which also earns the points the user
            # wants - provably earns every one of them, watching the
            # discovered stream as well adds nothing. A discovered key
            # is never None here, so it holds no channel-restricted
            # campaign. Cheapest checks first: this runs for every
            # online streamer while a discovered pick holds the slot,
            # and _is_drop_stream prunes badge campaigns as a side
            # effect.
            if index in discovered_tiers or discovered_index not in discovered_tiers:
                return False
            discovered_key = _drop_campaign_key(discovered_index)
            if discovered_key is None or _streamer_drops_demoted(index):
                return False
            if not _channel_earned_campaign_ids(index) >= discovered_key:
                return False
            return _is_drop_stream(index)

        streak_priority = Priority.STREAK in priority

        def _may_share_drop_slot(index, kept_index):
            # Two streams earning exactly the same unrestricted
            # campaigns cannot split progress, and a pending watch
            # streak (when the user prioritizes streaks) is only a
            # few minutes of overlap - both may stay watched.
            if streak_priority and index not in discovered_tiers:
                if self._has_pending_watch_streak(streamers_snapshot[index], now):
                    return True
            kept_key = _drop_campaign_key(kept_index)
            return kept_key is not None and kept_key == _drop_campaign_key(index)

        drop_streams = [
            index for index in filtered_streamers_watching if _is_drop_stream(index)
        ]
        kept_drop_index = min(drop_streams, key=_drop_rank) if drop_streams else None
        # Hand a discovered Drops pick's slot to a configured or
        # followed stream that covers it, whichever ranks higher and
        # even when that stream missed the initial capped selection:
        # it takes the discovered pick's place, so no other watched
        # stream is displaced.
        if kept_drop_index in discovered_tiers:
            watching = set(filtered_streamers_watching)
            # In streamers_index order, so min() breaks ties by it.
            covering = [
                index
                for index in streamers_index
                if _covers_discovered(index, kept_drop_index)
            ]
            if covering:
                replaced_discovered_index = kept_drop_index
                kept_drop_index = min(
                    covering,
                    key=lambda index: (
                        index not in watching,
                        _drop_rank(index),
                    ),
                )
                if kept_drop_index not in watching:
                    filtered_streamers_watching[
                        filtered_streamers_watching.index(replaced_discovered_index)
                    ] = kept_drop_index
                    drop_streams.append(kept_drop_index)
        # Every discovered Drops stream the kept stream covers,
        # including a pick it just replaced.
        redundant = [
            index
            for index in drop_streams
            if _covers_discovered(kept_drop_index, index)
        ]
        conflicting = [
            index
            for index in drop_streams
            if index != kept_drop_index
            and index not in redundant
            and not _may_share_drop_slot(index, kept_drop_index)
        ]
        evicted = set(redundant) | set(conflicting)
        filtered_streamers_watching = [
            index for index in filtered_streamers_watching if index not in evicted
        ]
        # A redundant pick stays the remembered pick, so stickiness
        # and discovery keep the same verified channel for when the
        # covering stream stops covering it.
        if kept_pick_index in conflicting:
            drop_pick_streamer = None
        # Redundant or conflicting streams come from drop_streams, so
        # kept_drop_index is set whenever either list is non-empty.
        covering_username = (
            streamers_snapshot[kept_drop_index].username
            if redundant and kept_drop_index is not None
            else None
        )

        def _describe(indexes):
            return ", ".join(
                f"{streamers_snapshot[index].username} "
                f"({streamer_source(index).name.lower()})"
                for index in indexes
            )

        drop_conflict_parts = []
        if redundant:
            drop_conflict_parts.append(
                f"{_describe([kept_drop_index])} already earns the same "
                "Drops campaign(s) as the discovered Drops pick: "
                f"skipping {_describe(redundant)}"
            )
        if conflicting:
            drop_conflict_parts.append(
                "Drops progress only counts on one channel at a time: "
                f"keeping {_describe([kept_drop_index])}, "
                f"skipping {_describe(conflicting)}"
            )
        # Discovery can offer a different channel for the same game
        # on each refresh; the redundant channels it offers are left
        # out of the key so the same decision is logged only once.
        drop_conflict_key = (
            (
                bool(redundant),
                streamers_snapshot[kept_drop_index].username,
                tuple(streamers_snapshot[index].username for index in conflicting),
            )
            if drop_conflict_parts and kept_drop_index is not None
            else None
        )

        # Use any freed slot for a points-only stream - never another
        # discovered stream, and never a second Drops stream unless it
        # may share the Drops slot with the kept one.
        for index in streamers_index:
            if len(filtered_streamers_watching) >= max_watch_amount:
                break
            if index in filtered_streamers_watching:
                continue
            if index in discovered_tiers:
                continue
            if _is_drop_stream(index):
                if kept_drop_index is None:
                    kept_drop_index = index
                elif not _may_share_drop_slot(index, kept_drop_index):
                    continue
            filtered_streamers_watching.append(index)
        streamers_watching = filtered_streamers_watching

        # Log the category/wildcard picks only once every slot decision is
        # final: a badge or higher-ranked Drops stream can evict the
        # discovered pick after it was chosen.
        final_watched = set(streamers_watching)
        return StreamSelection(
            watching=streamers_watching,
            streamer_source=streamer_source,
            is_drop_stream=_is_drop_stream,
            category_expiration=category_expiration,
            category=DropTierPick(
                watched_pick=(
                    best_category_index
                    if best_category_index in final_watched
                    else None
                ),
                ordered=indexes_by_source[StreamerSource.CATEGORIES],
                reason=category_hold_reason,
                slotted_candidates=category_candidates,
                covered_by=(
                    covering_username if best_category_index in redundant else None
                ),
            ),
            wildcard=DropTierPick(
                watched_pick=(
                    best_wildcard_category_index
                    if best_wildcard_category_index in final_watched
                    else None
                ),
                ordered=indexes_by_source[StreamerSource.WILDCARD_CATEGORIES],
                reason=wildcard_hold_reason,
                slotted_candidates=wildcard_category_candidates,
                covered_by=(
                    covering_username
                    if best_wildcard_category_index in redundant
                    else None
                ),
            ),
            drop_pick_streamer=drop_pick_streamer,
            drop_slot_covering_streamer=covering_username,
            drop_conflict_key=drop_conflict_key,
            drop_conflict_message=(
                "; ".join(drop_conflict_parts) if drop_conflict_parts else None
            ),
        )
