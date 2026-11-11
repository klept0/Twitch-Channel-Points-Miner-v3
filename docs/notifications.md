# Notifications

[Back to the README](../README.md)

Notifications are event-filtered copies of selected log messages. Add provider
objects to `LoggerSettings`; leave unused providers as `None`. Every provider
requires an `events` list, and the same event can be sent through several
providers.

```python
from TwitchChannelPointsMiner.classes.Settings import Events
from TwitchChannelPointsMiner.classes.Telegram import Telegram
from TwitchChannelPointsMiner.logger import LoggerSettings

logger_settings = LoggerSettings(
    save=True,
    auto_clear=True,
    log_retention_days=7,
    telegram=Telegram(
        chat_id=123456789,
        token="YOUR_BOT_TOKEN",
        events=[Events.STREAMER_OFFLINE, Events.DROP_CLAIM],
        message_thread_id=987,
    ),
)
```

With `auto_clear=True` (the default), the active `logs/<username>.log` file is
rolled at local midnight on Windows and Linux. Daily archives use a date suffix,
and `log_retention_days` controls how many are retained (7 by default). Set
`auto_clear=False` to preserve the existing timestamped-file-per-start behavior.

Assign this object to `MINER_CONFIG["logger_settings"]`. The complete
[configuration template](../config.example.py) shows all providers together.
Placeholder values are ignored; use real credentials and never commit them.

| Provider | Destination | Class |
| :-- | :-- | :-- |
| Telegram | Bot chat | `Telegram` |
| Discord | Channel webhook | `Discord` |
| Generic webhook | HTTP endpoint | `Webhook` |
| Matrix | Matrix room | `Matrix` |
| Pushover | Pushover account or group | `Pushover` |
| Gotify | Self-hosted Gotify application | `Gotify` |
| Email | Addresses reached through SMTP | `Email` |
| ntfy | ntfy topic (hosted or self-hosted) | `Ntfy` |
| Apprise | Any of 100+ services, one URL each | `Apprise` |

Telegram, Discord and Pushover split a message that is too long to send in
one piece into consecutive messages, breaking at line ends where possible. A
notification is sent as at most ten messages; the tenth says how many more
were left out. On Telegram and Pushover only the first message alerts as
configured: later ones are sent silently on Telegram and at quiet priority on
Pushover. Discord posts every message normally.

Delivery failures are logged and do not stop mining. Enable
`console_username` when several accounts share a destination. The analytics
dashboard can send a test message for each enabled provider.

## Telegram

Create a bot with [@BotFather](https://t.me/botfather), then obtain the numeric
destination `chat_id` (for example with [@getmyid_bot](https://t.me/getmyid_bot)).

| Key | Type | Default | Description |
| :-- | :-- | :-- | :-- |
| `chat_id` | int | Required | Numeric Telegram chat ID. |
| `token` | str | Required | Bot token issued by BotFather. |
| `events` | list | Required | Events to send. |
| `disable_notification` | bool | `False` | Send without sound or vibration. Later messages of a split notification are always silent. |
| `message_thread_id` | int or None | `None` | Optional Telegram forum topic ID for supergroup topics. |

```python
Telegram(
    chat_id=123456789,
    token="YOUR_BOT_TOKEN",
    events=[Events.STREAMER_ONLINE, Events.CHAT_MENTION],
    disable_notification=True,
    message_thread_id=987,
)
```

## Discord

In the destination channel, open **Edit Channel > Integrations > Webhooks**,
create a webhook, and copy its URL.

| Key | Type | Default | Description |
| :-- | :-- | :-- | :-- |
| `webhook_api` | str | Required | Discord webhook URL. |
| `events` | list | Required | Events to send. |

```python
Discord(
    webhook_api="https://discord.com/api/webhooks/ID/TOKEN",
    events=[Events.STREAMER_OFFLINE, Events.BET_LOSE],
)
```

## Generic Webhook

`Webhook` sends `event_name` and `message` as URL query parameters for `GET`, or
as form fields in the request body for `POST`.

| Key | Type | Default | Description |
| :-- | :-- | :-- | :-- |
| `endpoint` | str | Required | Destination URL. |
| `method` | str | Required | `POST` or `GET`. |
| `events` | list | Required | Events to send. |
| `timeout` | float | `10` | Request timeout in seconds. |

```python
Webhook(
    endpoint="https://example.com/webhook",
    method="POST",
    events=[Events.DROP_CLAIM, Events.CONFIGURATION],
    timeout=5,
)
```

## ntfy

`Ntfy` publishes plain-text notifications to [ntfy](https://ntfy.sh/). The
default server is `https://ntfy.sh`; set `server_url` for a self-hosted server.
Topic names act like passwords when access control is not enabled, so use a
hard-to-guess topic and do not commit it. Protected topics can use an access
token.

| Key | Type | Default | Description |
| :-- | :-- | :-- | :-- |
| `topic` | str | Required | Destination topic name. |
| `events` | list | Required | Events to send. |
| `server_url` | str | `"https://ntfy.sh"` | Hosted or self-hosted ntfy server URL. |
| `token` | str or None | `None` | Optional ntfy access token. |
| `priority` | int or None | `None` | Optional ntfy priority from 1 through 5. |
| `tags` | list, tuple, str, or None | `None` | Optional ntfy tags. |
| `timeout` | float | `10` | Request timeout in seconds. |

```python
from TwitchChannelPointsMiner.classes.Ntfy import Ntfy

Ntfy(
    topic="YOUR_NTFY_TOPIC",
    token="YOUR_ACCESS_TOKEN",
    priority=3,
    tags=["twitch"],
    events=[Events.STREAMER_OFFLINE, Events.DROP_CLAIM],
)
```

## Apprise

`Apprise` sends notifications through [Apprise](https://github.com/caronc/apprise),
which supports more than 100 services. Each destination is one URL, such as
`discord://`, `tgram://` or `ntfy://`; see the
[Apprise wiki](https://github.com/caronc/apprise/wiki) for every format.

Apprise is optional. Install it with `pip install apprise` only if you use this
notifier. Without it, the miner logs one warning and keeps running.

| Key | Type | Default | Description |
| :-- | :-- | :-- | :-- |
| `urls` | str or list | Required | One or more Apprise service URLs. |
| `events` | list | Required | Events to send. |
| `title` | str | `"Twitch Channel Points Miner"` | Notification title. |

```python
from TwitchChannelPointsMiner.classes.Apprise import Apprise

Apprise(
    urls=["discord://WEBHOOK_ID/WEBHOOK_TOKEN", "tgram://BOT_TOKEN/CHAT_ID"],
    events=[Events.DROP_CLAIM, Events.STREAMER_OFFLINE],
)
```

## Matrix

Matrix logs in when configured, then sends plain-text messages to the room.

| Key | Type | Default | Description |
| :-- | :-- | :-- | :-- |
| `username` | str | Required | Username accepted by the homeserver. |
| `password` | str | Required | Matrix account password. |
| `homeserver` | str | Required | Hostname without `https://`, such as `matrix.org`. |
| `room_id` | str | Required | Destination room ID, such as `!roomid:matrix.org`. |
| `events` | list | Required | Events to send. |

```python
Matrix(
    username="twitch_miner",
    password="YOUR_MATRIX_PASSWORD",
    homeserver="matrix.org",
    room_id="!roomid:matrix.org",
    events=[Events.STREAMER_OFFLINE, Events.DROP_CLAIM],
)
```

The account must already be able to post in the room. Authentication failure is
logged at startup and disables Matrix delivery for that run.

## Pushover

Create an application at [Pushover](https://pushover.net/), then use the account
user key and application API token.

| Key | Type | Default | Description |
| :-- | :-- | :-- | :-- |
| `userkey` | str | Required | Pushover user or group key. |
| `token` | str | Required | Application API token. |
| `priority` | int | Required | [Pushover message priority](https://pushover.net/api#priority). Later messages of a split notification go at quiet priority (-1), or -2 when the configured priority is -2 or lower. |
| `sound` | str | Required | [Pushover sound name](https://pushover.net/api#sounds). |
| `events` | list | Required | Events to send. |

```python
Pushover(
    userkey="YOUR_USER_KEY",
    token="YOUR_APPLICATION_TOKEN",
    priority=0,
    sound="pushover",
    events=[Events.CHAT_MENTION, Events.DROP_CLAIM],
)
```

## Gotify

Create an application in Gotify and use its message endpoint, including the
application token.

| Key | Type | Default | Description |
| :-- | :-- | :-- | :-- |
| `endpoint` | str | Required | Message URL, such as `https://gotify.example/message?token=TOKEN`. |
| `priority` | int | Required | Priority passed to Gotify. |
| `events` | list | Required | Events to send. |

```python
Gotify(
    endpoint="https://gotify.example/message?token=YOUR_TOKEN",
    priority=8,
    events=[Events.STREAMER_OFFLINE, Events.BET_LOSE],
)
```

## Email / SMTP

Email uses Python's built-in SMTP support and accepts one recipient or a list or
tuple of recipients.

| Key | Type | Default | Description |
| :-- | :-- | :-- | :-- |
| `host` | str | Required | SMTP server hostname. |
| `port` | int | Required | SMTP server port. |
| `sender` | str | Required | Address placed in the `From` header. |
| `recipients` | str, list, or tuple | Required | One recipient or a list/tuple of recipients. |
| `events` | list | Required | Events to send. |
| `username` | str or None | `None` | SMTP login username. |
| `password` | str or None | `None` | SMTP login password. |
| `use_ssl` | bool | `False` | Use implicit TLS from connection start. |
| `starttls` | bool | `True` | Upgrade a plain connection with STARTTLS. |
| `timeout` | float | `15` | Connection timeout in seconds. |

```python
Email(
    host="smtp.example.com",
    port=587,
    username="miner@example.com",
    password="YOUR_SMTP_PASSWORD",
    sender="miner@example.com",
    recipients=["you@example.com"],
    events=[Events.DAILY_REPORT, Events.CONFIGURATION],
    starttls=True,
)
```

`use_ssl` and `starttls` cannot both be enabled. Use `use_ssl=True,
starttls=False` for implicit TLS, commonly on port 465. For scheduled summaries,
subscribe to `DAILY_REPORT` and set `daily_report=True`. Report progress is saved
per account under `logs/.state/`, including Drop progress and the points of
channels added or removed during the day, so restarting the miner doesn't drop
them from the next report. The report also lists finished Drops that Twitch
won't deliver until the campaign's game account is linked, with the link URL,
every day until the account is linked. An overdue report is sent after startup
finishes refreshing streamer balances.

# Events

Import `Events` from `TwitchChannelPointsMiner.classes.Settings` and select any
of these values in each provider's `events` list.

| Event | Sent when |
| :-- | :-- |
| `UPDATE_AVAILABLE` | The update checker finds a newer release. This alert is forced through every configured provider when update checking is enabled. |
| `DAILY_REPORT` | The scheduled point and Drop activity summary is generated. Requires `daily_report=True`. |
| `STREAMER_ONLINE` | A tracked streamer goes live. |
| `STREAMER_OFFLINE` | A tracked streamer goes offline. |
| `GAIN_FOR_RAID` | Channel points are earned for joining a raid. |
| `GAIN_FOR_CLAIM` | Channel points are earned from a claimed bonus. |
| `GAIN_FOR_WATCH` | Channel points are earned for watch time. |
| `GAIN_FOR_WATCH_STREAK` | Channel points are earned from a watch-streak reward. |
| `BET_WIN` | A prediction wins and its result is credited. |
| `BET_LOSE` | A prediction loses. |
| `BET_REFUND` | A prediction is canceled or refunded. |
| `BET_FILTERS` | A prediction is skipped because a configured filter does not match. |
| `BET_GENERAL` | General prediction decisions or status messages are produced. |
| `BET_FAILED` | A prediction cannot be placed. |
| `BET_START` | A prediction window opens. |
| `BONUS_CLAIM` | The periodic channel-points bonus is claimed. |
| `MOMENT_CLAIM` | A Twitch Moment is claimed. |
| `JOIN_RAID` | The miner follows a raid to another channel. |
| `DROP_CLAIM` | A completed Drop reward is claimed. |
| `DROP_STATUS` | Drop discovery, eligibility, or progress status is reported. This can be verbose. |
| `DROP_LINK_REQUIRED` | A finished Drop can't be claimed because the campaign's game account isn't linked to Twitch. Sent once per campaign per run (so again after a restart) with the link URL, and once more when its Drops have been claimed. |
| `CHAT_MENTION` | The configured username is mentioned in joined chat. Chat presence must be enabled. |
| `GIFTED_SUB` | Someone gifts this account a sub in a joined chat. Chat presence must be enabled. Anonymous gifts are reported as "An Anonymous Gifter". |
| `CONFIGURATION` | Startup, reload, authentication, or other configuration information requires attention. Critical authentication alerts may be forced through configured providers. |
| `WATCH_STATUS` | Never sent. It only names the *Watching for points* status line for `ColorPalette`; adding it to a notifier's `events` has no effect. |
