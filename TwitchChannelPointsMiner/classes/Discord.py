import re
from functools import partial
from textwrap import dedent

import requests

from TwitchChannelPointsMiner.classes.NotificationError import (
    MAX_NOTIFICATION_PARTS,
    notification_prefix,
    send_parts,
)
from TwitchChannelPointsMiner.classes.Settings import Events
from TwitchChannelPointsMiner.utils import split_message_lines

# Discord rejects webhook posts whose ``content`` exceeds this many characters
# (counted in UTF-16 code units).
DISCORD_CONTENT_LIMIT = 2000

# Embed descriptions allow more text than ``content``.
DISCORD_EMBED_DESCRIPTION_LIMIT = 4096

DEFAULT_USERNAME = "Twitch Channel Points Miner"
DEFAULT_AVATAR_URL = "https://i.imgur.com/X9fEkhT.png"

# Largest colour Discord accepts: a 24-bit RGB value.
MAX_DISCORD_COLOR = 0xFFFFFF

# Stands in for backticks when escaping them would need a fence that crowds the
# message out of each post.
BACKTICK_LOOKALIKE = "\u02cb"


class Discord(object):
    __slots__ = [
        "webhook_api",
        "events",
        "username",
        "avatar_url",
        "embed",
        "title",
        "color",
        "colors",
    ]

    def __init__(
        self,
        webhook_api: str,
        events: list,
        username: str | None = None,
        avatar_url: str | None = None,
        embed: bool = False,
        title: str | None = None,
        color: int | None = None,
        colors: dict | None = None,
    ):
        self.webhook_api = webhook_api
        self.events = [str(e) for e in events]
        self.username = _check_text("username", username)
        self.avatar_url = _check_text("avatar_url", avatar_url)
        self.embed = embed
        self.title = _check_text("title", title)
        self.color = None if color is None else _check_color(color)
        if colors is None:
            self.colors = {}
        elif isinstance(colors, dict):
            # Keys may be Events members (str() gives the name) or plain names.
            self.colors = {
                str(key): _check_color(value) for key, value in colors.items()
            }
        else:
            raise TypeError(f"Discord colors must be a dict or None, got {colors!r}.")

    def send(self, message: str, event: Events) -> tuple[bool, str | None]:
        if str(event) in self.events:
            bodies = self._build_bodies(dedent(message).strip(), event)
            session = requests.Session()
            try:
                return send_parts("Discord", bodies, partial(self._post, session))
            finally:
                session.close()
        return False, "This event is not enabled for Discord."

    def _post(self, session: requests.Session, body: dict) -> None:
        # ``wait=true`` makes Discord confirm each part before the next is sent,
        # so split notifications are neither dropped nor reordered.
        response = session.post(
            url=_without_wait(self.webhook_api),
            params={"wait": "true"},
            timeout=(5, 15),
            **body,
        )
        response.raise_for_status()

    def _build_bodies(self, message: str, event: Events) -> list[dict]:
        """Return the keyword arguments for each post, in order."""
        username = DEFAULT_USERNAME if self.username is None else self.username
        avatar_url = DEFAULT_AVATAR_URL if self.avatar_url is None else self.avatar_url
        if not self.embed:
            return [
                {
                    "data": {
                        "content": content,
                        "username": username,
                        "avatar_url": avatar_url,
                    }
                }
                for content in self._format_contents(message)
            ]

        name = str(event)
        color = self.colors.get(name, self.color)
        bodies = []
        for index, description in enumerate(self._split_embeds(message)):
            embed = {"description": description}
            if index == 0 and self.title:
                # str.replace rather than str.format, so braces cannot raise.
                embed["title"] = self.title.replace("{event}", name)
            if color is not None:
                embed["color"] = color
            bodies.append(
                {
                    "json": {
                        "username": username,
                        "avatar_url": avatar_url,
                        "embeds": [embed],
                    }
                }
            )
        return bodies

    @staticmethod
    def _split_embeds(message: str) -> list[str]:
        """Split the message into embed descriptions, without a code fence."""
        return Discord._split(
            message, DISCORD_EMBED_DESCRIPTION_LIMIT, notice_separator="\n"
        )

    @staticmethod
    def _split(message: str, limit: int, notice_separator: str) -> list[str]:
        return split_message_lines(
            message,
            limit,
            notification_prefix(),
            max_pieces=MAX_NOTIFICATION_PARTS,
            notice_separator=notice_separator,
        )

    @staticmethod
    def _format_contents(message: str) -> list[str]:
        """Wrap the message in a code fence, split so each post fits the limit.

        The fence is computed once from the whole message so every chunk uses
        the same fence and no chunk can be broken by backticks in the content.
        """
        longest_run = max(map(len, re.findall("`+", message)), default=0)
        multiline = "\n" in message
        fence_length = max(3 if multiline else 1, longest_run + 1)
        if 2 * fence_length + 2 > DISCORD_CONTENT_LIMIT // 2:
            # A fence long enough to escape this message would leave little or
            # no room for the message in each post, spraying it over many
            # near-empty posts: replace the backticks with a look-alike instead.
            message = message.replace("`", BACKTICK_LOOKALIKE)
            longest_run = 0
            fence_length = 3 if multiline else 1

        fence = "`" * fence_length
        if multiline:
            opening, closing = f"{fence}\n", f"\n{fence}"
        elif longest_run:
            opening, closing = f"{fence} ", f" {fence}"
        else:
            opening, closing = "`", "`"
        limit = DISCORD_CONTENT_LIMIT - len(opening) - len(closing)
        chunks = Discord._split(
            message,
            limit,
            # Inline code must stay on one line; a code block takes a new one.
            notice_separator="\n" if multiline else " ",
        )
        return [f"{opening}{chunk}{closing}" for chunk in chunks]


def _check_text(name: str, value):
    if value is not None and not isinstance(value, str):
        raise TypeError(f"Discord {name} must be a str or None, got {value!r}.")
    return value


def _check_color(value):
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= MAX_DISCORD_COLOR
    ):
        raise ValueError(
            f"Discord color must be an int from 0 to 0xFFFFFF, got {value!r}."
        )
    return value


def _without_wait(webhook_url: str) -> str:
    """Drop any ``wait`` query parameter so ``params`` can set it.

    requests appends ``params`` to an existing query string instead of
    replacing keys, which would leave a configured ``wait=false`` in place.
    A value that is not a string is returned unchanged, so requests reports
    it as an invalid URL.
    """
    if not isinstance(webhook_url, str):
        return webhook_url
    base, _, query = webhook_url.partition("?")
    kept = [item for item in query.split("&") if item and item.split("=")[0] != "wait"]
    return f"{base}?{'&'.join(kept)}" if kept else base
