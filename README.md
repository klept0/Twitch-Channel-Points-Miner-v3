![Twitch Channel Points Miner](assets/banner.png)

# Twitch Channel Points Miner v3

[![Tests](https://github.com/klept0/Twitch-Channel-Points-Miner-v3/actions/workflows/tests.yml/badge.svg)](https://github.com/klept0/Twitch-Channel-Points-Miner-v3/actions/workflows/tests.yml) [![License](https://img.shields.io/github/license/klept0/Twitch-Channel-Points-Miner-v3?style=flat&color=black)](LICENSE) [![GitHub last commit](https://badgen.net/github/last-commit/klept0/Twitch-Channel-Points-Miner-v3?icon=github&iconColor=fff&color=FFFFE0)](https://github.com/klept0/Twitch-Channel-Points-Miner-v3/commits/master)

A script that watches Twitch streams for you and earns channel points. It waits
for streamers to go live (+_450 points_ when the stream starts), claims the
bonus chest (_+50 points_), follows raids (_+250 points_), and can earn Drops
and play predictions. Read more about channel points in
[Twitch's guide](https://help.twitch.tv/s/article/channel-points-guide).

This is a fork of
[zarmstrong/Twitch-Channel-Points-Miner-v3](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3).
It tracks upstream and adds a few features of its own; see
[What this fork adds](#what-this-fork-adds).

## Contents

- [Features](#features)
- [What this fork adds](#what-this-fork-adds)
- [Before you start](#before-you-start)
- [Install](#install)
  - [Source checkout](#source-checkout)
  - [Docker](#docker)
  - [Windows executable](#windows-executable)
- [First run](#first-run)
- [Configuration](#configuration)
- [Documentation](#documentation)
- [Contributing](#contributing)
- [License](#license)
- [Acknowledgments](#acknowledgments)
- [Support](#support)

## Features

- 👀 **Channel watching**: watches eligible channels and claims channel-point bonuses.
- ⏰ **Streamers and raids**: waits for configured streamers to go live and follows eligible raids.
- 🏆 **Priority**: picks channels by watch streaks, Drops, subscriptions, favorites, configured order, or channel-point balance.
- 🎮 **Category discovery**: finds live channels from configured game categories and active Drop campaigns.
- 🎁 **Drops and Moments**: tracks Drop progress, including channel campaigns from `DropChannelCampaignsProgress`, and claims Drop rewards and Moments.
- 🎲 **Predictions**: bets on channel-point predictions with configurable strategies, spending limits, filters, and timing.
- ⚙️ **Per-streamer settings**: per-streamer overrides, follower imports, and blacklists.
- 💬 **IRC chat**: joins chat and notifies you when your username is mentioned.
- 🔔 **Notifications**: sends selected events to Telegram, Discord, Matrix, Pushover, Gotify, ntfy, email, or a generic webhook.
- 📜 **Logs**: colorized console logs, rotating log files, compact logging, and a graceful-shutdown report.
- 📊 **Analytics**: an optional web dashboard for point history, Drops, configuration, and logs.
- 🔄 **Live reload**: applies some configuration changes without a restart and migrates older configurations automatically.

## What this fork adds

These features are in this fork's `master` branch but not in upstream:

| Feature | What it does | Docs |
| :-- | :-- | :-- |
| 🧰 Supervisor | An optional cross-platform supervisor for source checkouts: restarts on crashes and on a schedule, applies `git` updates, and sends alerts. | [Supervisor](docs/supervisor.md) |
| ⚡ Fast Drop claims | Claims Drops as soon as PubSub reports them claimable, including on channels the miner isn't tracking. | |
| 🛟 Raw-query fallback | Resends a GraphQL query as raw text when Twitch rejects its persisted hash. | |

> [!IMPORTANT]
> The Docker image and the Windows executable are built by upstream. They do
> not include these additions. To use them, run a
> [source checkout](#source-checkout).

## Before you start

> [!WARNING]
> This project comes with no guarantee or warranty. You are responsible for
> whatever happens from using it. You can get soft or hard banned if you are
> not careful. This is a personal project and is not affiliated with Twitch.

### Limits

- **Two channels at a time.** Twitch only credits watching two channels at
  once. Set `streams_watched` in `MINER_CONFIG` to `1` or `2` (the default is
  `2`). The highest-priority streamers get the slots.
- **Ads for Turbo users.** Turbo users have reported ads when two mined streams
  run continuously, especially while also watching Twitch normally
  ([upstream issue #787](https://github.com/rdavydov/Twitch-Channel-Points-Miner-v2/issues/787)).
  Setting `streams_watched` to `1` reduces this, but Twitch does not document
  the behavior, so it is not a guarantee.
- **Priority order.** Write the `STREAMERS` list in priority order, from first
  to last. With `followers=True`, you can sort followers by follow date
  (`ASC` or `DESC`).
- **Drop segment requests.** Since October 2026, Twitch only credits
  time-based Drop progress while a stream's media segments are being requested.
  For each watched stream with Drops to earn, the miner sends a lightweight
  `HEAD` request for every new HLS segment. No audio or video is downloaded,
  streams watched only for channel points are skipped, and no configuration is
  needed.

## Install

You need a Twitch account and one of the following.

| Method | Best for | Includes fork additions |
| :-- | :-- | :-- |
| [Source checkout](#source-checkout) | Any OS; this fork's features | ✅ Yes |
| [Docker](#docker) | Servers, NAS devices, unattended use | ❌ No (upstream image) |
| [Windows executable](#windows-executable) | Windows 10/11 without Python | ❌ No (upstream build) |

### Source checkout

Requires Git and Python 3.11 through 3.14.

```sh
git clone https://github.com/klept0/Twitch-Channel-Points-Miner-v3.git
cd Twitch-Channel-Points-Miner-v3
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
# Only for the optional Apprise notifier:
# pip install -r requirements-optional.txt
mkdir -p config && cp config.example.py config/config.py
```

To keep it running unattended, with automatic restarts and updates, see the
[Supervisor](docs/supervisor.md).

### Docker

The upstream image `zacharmstrong/twitch-channel-points-miner` runs on
`linux/amd64` and `linux/arm64`. A minimal `compose.yml`:

```yaml
services:
  miner:
    image: zacharmstrong/twitch-channel-points-miner:latest
    restart: "no"
    stop_grace_period: 60s
    stdin_open: true
    tty: true
    environment:
      - TERM=xterm-256color
      - TZ=America/Denver
    volumes:
      - ./analytics:/usr/src/app/analytics
      - ./cookies:/usr/src/app/cookies
      - ./logs:/usr/src/app/logs
      - ./config:/usr/src/app/config
    ports:
      - "54455:54455"
```

The first `docker compose up` creates `config/config.py` and exits. Edit it,
change `restart: "no"` to `restart: unless-stopped`, run `docker compose up`
once more to sign in to Twitch, then use `docker compose up -d`.

The [Docker guide](docs/docker.md) covers the plain Docker CLI, multiple
accounts, Portainer, time zones, and graceful shutdowns.

### Windows executable

Upstream publishes a Windows installer and a portable ZIP that need no Python
or Git. Download `TwitchChannelPointsMiner-<version>-Setup.exe` from the
[upstream Releases page](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/releases),
run it, and fill in the **Config** tab when the app opens.

The [Windows guide](docs/windows.md) covers the standard and portable builds,
updates, and troubleshooting.

## First run

Edit `config/config.py` (see [Configuration](#configuration)), then start the
miner:

```sh
python -m TwitchChannelPointsMiner.runner --config-dir ./config
```

On the first start, the miner asks you to open https://www.twitch.tv/activate
and enter a code it prints. The session is then saved in `cookies/`.

> [!CAUTION]
> Keep `config/` and `cookies/` private. Never commit or share account names,
> cookies, webhook URLs, or notification credentials.

## Configuration

All run methods read `config/config.py`. A minimal configuration:

```python
from TwitchChannelPointsMiner.classes.Settings import FollowersOrder

MINER_CONFIG = {
    "username": "your-twitch-username",
    "enable_analytics": False,
}

STREAMERS = ["streamer1", "streamer2"]

MINE_CONFIG = {
    "followers": False,
    "followers_order": FollowersOrder.ASC,
    "blacklist": [],
    "categories": [],
}

# Set to a dictionary to start the analytics server, or leave as None.
ANALYTICS_CONFIG = None
```

The file is normal Python, so import any enums or settings classes its values
use. [config.example.py](config.example.py) is the complete annotated example,
and the [configuration guide](docs/configuration.md) explains every section.

## Documentation

| Guide | Covers |
| :-- | :-- |
| [Configuration](docs/configuration.md) | `MINER_CONFIG`, `STREAMERS`, `MINE_CONFIG`, `ANALYTICS_CONFIG`, runner behavior, live reload |
| [Settings](docs/settings.md) | Priority, `LoggerSettings`, color palette, `StreamerSettings` |
| [Notifications](docs/notifications.md) | Telegram, Discord, webhook, ntfy, Apprise, Matrix, Pushover, Gotify, email, events |
| [Predictions](docs/predictions.md) | `BetSettings`, strategies, filters, delay modes |
| [Categories and Drops](docs/categories-and-drops.md) | Category discovery, campaign filtering, wildcard categories |
| [Analytics](docs/analytics.md) | Dashboard, security, HTTPS reverse proxy, storage |
| [Logs](docs/logs.md) | Log formats and the graceful-shutdown report |
| [Docker](docs/docker.md) | Compose, CLI, multiple accounts, Portainer, shutdowns |
| [Windows](docs/windows.md) | Installer and portable builds, setup, updates, troubleshooting |
| [Supervisor](docs/supervisor.md) | Unattended source checkouts (fork only) |
| [Migration](docs/migration.md) | Moving from `run.py`, data versions, legacy cookies |
| [Troubleshooting](docs/troubleshooting.md) | Common problems and fixes |

## Contributing

Issues and pull requests are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md)
first, and see the [testing guide](tests/README.md) for setup and test commands.

- Code is Black-formatted; the Black version is pinned in `requirements-dev.txt`.
- Run the tests with `python -m pytest`.
- Use Conventional Commit titles, such as `fix: handle empty category list`.
- Changes that aren't fork-specific belong upstream at
  [zarmstrong/Twitch-Channel-Points-Miner-v3](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3).

## License

Licensed under the [GNU General Public License v3.0](LICENSE).

## Acknowledgments

- Main idea: [gottagofaster236/Twitch-Channel-Points-Miner](https://github.com/gottagofaster236/Twitch-Channel-Points-Miner)
- Based on: [Tkd-Alex/Twitch-Channel-Points-Miner-v2](https://github.com/Tkd-Alex/Twitch-Channel-Points-Miner-v2)
- Previous maintainer: [Roman Davydov](https://github.com/rdavydov), whose [v2 fork](https://github.com/rdavydov/Twitch-Channel-Points-Miner-v2) this project builds on
- Upstream v3: [zarmstrong/Twitch-Channel-Points-Miner-v3](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3) by [zarmstrong](https://github.com/zarmstrong)

## Support

If this helps you, give it a ⭐️. Found a bug or have a question?
[Open an issue](https://github.com/klept0/Twitch-Channel-Points-Miner-v3/issues).

To support this fork's maintainer, klept0:

[![Support me on Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/its_klept0)

To support the upstream maintainer, zarmstrong:

[![Support me on Ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/B0B61NBJ0)

| Currency | Wallet address |
| :-- | :-- |
| Bitcoin (BTC) | `bc1qdpg70u7u6fganwtddf9z3uwugkxkpltre4cva3` |
| Ethereum (ETH) | `0x926fce1Eb65e867Af751a9cB75394DF001b58DF0` |
| Dogecoin (DOGE) | `DHo26McxeVLFwQmRXdUH7LyHsdEUoVFvga` |
