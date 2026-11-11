# Settings

[Back to the README](../README.md)

Most settings are documented inline in [config.example.py](../config.example.py).
The `priority` option controls which eligible streamers receive the available
watch slots. It accepts one `Priority` value or an ordered list. Include at least
one fallback such as `ORDER`, `POINTS_ASCENDING`, or `POINTS_DESCENDING`; using
only `STREAK`, for example, leaves no selection rule after available streaks have
been handled.

Available values:
 - `STREAK` - Catch the watch streak from all streamers
 - `FAVORITE` - Prioritize streamers with `StreamerSettings(favorite=True)`
 - `DROPS` - Claim all drops from streamers with drops tags enabled
 - `SUBSCRIBED` - Prioritize streamers you're subscribed to (higher subscription tiers are mined first)
 - `ORDER` - Following the order of the list
 - `POINTS_ASCENDING` - On top the streamers with the lowest points
 - `POINTS_DESCENDING` - On top the streamers with the highest points

Priorities can be combined in order. Avoid contradictory fallback rules such as
`ORDER` and `POINTS_ASCENDING` in the same list.

Set `points_limit` in `StreamerSettings` to stop watching a streamer once its
channel-points balance reaches that value. A pending watch streak can temporarily
bypass the limit so its reward is not missed. Set `favorite=True` per streamer
and include `Priority.FAVORITE` to reserve watch slots for favorites first.

## LoggerSettings

| Key | Type | Default | Description |
| :-- | :-- | :-- | :-- |
| `save` | bool | `True` | Save logs to rotating files. |
| `less` | bool | `False` | Use compact log formatting and shorter messages. |
| `console_format` | str or `LoggingFormat` | `"text"` | Console log format: `"text"` or `"json"`. JSON writes one object per line with an ISO 8601 timestamp (offset included), the account `username`, and the `event` name when present. `emoji`, `colored`, and `date_format` only affect `"text"`; `less` also shortens the messages themselves, so it changes the JSON `message` field too. |
| `console_level` | logging level | `logging.INFO` | Minimum level written to the console. Notifications are not affected; each notifier's `events` list decides what it sends. |
| `console_username` | bool | `False` | Include the account username in console and notification messages. |
| `time_zone` | str or None | `None` | Log time zone, such as `America/Denver`. |
| `date_format` | str | `"dd/mm/yy"` | Date format used in logs and analytics. Use `dd`, `mm`, and `yy` or `yyyy`, separated by `/`, `-`, or `.`. |
| `file_format` | str or `LoggingFormat` | `"text"` | Log file format: `"text"` or `"json"`, with the same fields as the console. `date_format` only affects `"text"`. The dashboard log viewer shows JSON lines as written. |
| `file_level` | logging level | `logging.DEBUG` | Minimum level written to log files. |
| `emoji` | bool | Platform-dependent | Enable emoji; defaults to disabled on Windows and enabled elsewhere. |
| `colored` | bool | `False` | Enable colored console output. |
| `auto_clear` | bool | `True` | Rotate logs daily and retain seven backups. |
| `color_palette` | ColorPalette | Default palette | Customize colors by event. |
| `telegram` | Telegram or None | `None` | Send selected events through Telegram. |
| `discord` | Discord or None | `None` | Send selected events through Discord. |
| `webhook` | Webhook or None | `None` | Send selected events to a generic HTTP endpoint. |
| `matrix` | Matrix or None | `None` | Send selected events to a Matrix room. |
| `pushover` | Pushover or None | `None` | Send selected events through Pushover. |
| `gotify` | Gotify or None | `None` | Send selected events to a Gotify server. |
| `email` | Email or None | `None` | Send selected events through an SMTP server. |
| `ntfy` | Ntfy or None | `None` | Publish selected events to an ntfy topic. |
| `apprise` | Apprise or None | `None` | Send selected events to any service Apprise supports. |
| `daily_report` | bool | `False` | Generate a daily channel-points and Drop activity report. |
| `daily_report_time` | str | `"00:00"` | Local delivery time for the daily report, in 24-hour `HH:MM` format. |

### Color Palette

`ColorPalette` customizes console colors by event name. Unspecified events use
`Fore.RESET`, while prediction wins and losses default to green and red. The
*Watching for points* status line has its own `WATCH_STATUS` key, which
defaults to green; it is a console and file log event only and is never sent
to notifiers. The palette applies only with `colored=True`; with
`colored=False` the *Watching for points* line is still green on the console,
as in earlier releases. Values
may be Colorama constants or case-insensitive color names: `BLACK`, `RED`,
`GREEN`, `YELLOW`, `BLUE`, `MAGENTA`, `CYAN`, `WHITE`, or `RESET`.

```python
from colorama import Fore
ColorPalette(
    STREAMER_ONLINE = Fore.GREEN,
    STREAMER_OFFLINE = Fore.RED,
    GAIN_FOR_RAID = Fore.YELLOW,
    GAIN_FOR_CLAIM = Fore.YELLOW,
    GAIN_FOR_WATCH = Fore.YELLOW,
    GAIN_FOR_WATCH_STREAK = Fore.YELLOW,
    BET_WIN = Fore.GREEN,
    BET_LOSE = Fore.RED,
    BET_REFUND = Fore.RESET,
    BET_FILTERS = Fore.MAGENTA,
    BET_GENERAL = Fore.BLUE,
    BET_FAILED = Fore.RED,
)
```

## StreamerSettings

| Key | Type | Default | Description |
| :-- | :-- | :-- | :-- |
| `make_predictions` | bool | `True` | Participate in channel-point predictions. |
| `follow_raid` | bool | `True` | Follow eligible raids. |
| `claim_drops` | bool | `True` | Accumulate Drop watch progress and claim available Drops. |
| `claim_moments` | bool | `True` | Claim available [Moments](https://help.twitch.tv/s/article/moments). |
| `watch_streak` | bool | `True` | Prioritize available watch-streak rewards. |
| `favorite` | bool | `False` | Prioritize this streamer when `Priority.FAVORITE` is configured. |
| `points_limit` | int or None | `None` | Stop watching at this balance, except for an eligible pending watch streak. |
| `community_goals` | bool | `False` | Contribute the maximum allowed points to community goals. |
| `bet` | BetSettings | Default settings | Configure prediction strategy, limits, filters, and timing. |
| `chat` | ChatPresence | `ONLINE` | Control when the miner joins IRC chat. |
| `gift_sub_thanks` | bool | `False` | Send a thank-you message in chat when someone gifts this account a sub there. Requires chat presence. |
| `gift_sub_message` | str | `"Thanks for the sub!"` | Thank-you text. Optionally add placeholders `{gifter}`, `{channel}` or `{tier}`, for example `"Thanks for the {tier} sub, {gifter}!"`. |

Allowed values for `chat` are:
- `ALWAYS` Join in IRC chat and never leave
- `NEVER` Never join IRC chat
- `ONLINE` Participate in IRC chat while the streamer is online (leave when offline)
- `OFFLINE` Participate in IRC chat while the streamer is offline (leave when online)

Gifted subs are only seen in chats the miner has joined, so `chat` must not be `NEVER` and the gift must arrive while the miner is in that chat. Check the channel's chat rules before turning on `gift_sub_thanks`: emote-only, followers-only or subscriber-only modes and bot rules can block or ban automated messages.
