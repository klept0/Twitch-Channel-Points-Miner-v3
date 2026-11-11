# Troubleshooting

[Back to the README](../README.md)

| Symptom | What to do |
| :-- | :-- |
| No category streamers are loaded | Confirm the game has an active, incomplete campaign and try its Twitch directory URL. Enable `print_open_drop_campaigns_on_load=True` to inspect detected campaigns. |
| A live channel is skipped | With `category_drops_enabled=True`, it must have the `DropsEnabled` tag. Restricted campaigns may also require the channel to be on the campaign's allowlist. |
| A forced streamer is skipped | Confirm the login is live, streaming the requested game, and has the Drops tag when required. |
| The chosen channels are unexpected | Check `category_sort`, `category_limit`, category order, and `CategoryCampaignOrder`. Restricted campaign allowlists take precedence over a normal category search. |
| A removed category still affects selection | Refresh only adds streamers; restart the miner to remove already-loaded category channels. |
| Category messages are missing | Set `category_log_level=logging.INFO` or `logging.DEBUG` and ensure the logger/console threshold permits that level. |
| Analytics omit channel point changes | Set `track_category_streamer_points=True`; it defaults to `False` for category-only channels. |
| Wildcard categories never activate | They only run once the preferred `categories` pass produces zero live streamers this cycle -- a long `categories` list with even one currently-live match will keep wildcard standing down. Confirm `wildcard_categories=True`, and check whether any configured category currently has a live matching channel, e.g. via `print_open_drop_campaigns_on_load=True` and `category_log_level=logging.DEBUG`. |
| A wildcard channel keeps getting swapped out | Set `wildcard_category_pin_active=True` (the default) so an already-tracked wildcard category isn't dropped just for falling out of the top `wildcard_category_limit` on a re-sort. |

| Emoji or symbols look broken | Set `LoggerSettings(emoji=False)` in `MINER_CONFIG["logger_settings"]`. See [LoggerSettings](settings.md#loggersettings). |
| The miner closes or reports an error after setup | Check the Console tab or the newest log file. Remove account names, cookies, tokens, and passwords before you share a log. |
