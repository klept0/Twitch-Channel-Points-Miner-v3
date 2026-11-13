import inspect

import pytest

from TwitchChannelPointsMiner.classes.Apprise import Apprise
from TwitchChannelPointsMiner.classes.Discord import Discord
from TwitchChannelPointsMiner.classes.Email import Email
from TwitchChannelPointsMiner.classes.Gotify import Gotify
from TwitchChannelPointsMiner.classes.Matrix import Matrix
from TwitchChannelPointsMiner.classes.NotificationError import Notifier, SendResult
from TwitchChannelPointsMiner.classes.Ntfy import Ntfy
from TwitchChannelPointsMiner.classes.Pushover import Pushover
from TwitchChannelPointsMiner.classes.Telegram import Telegram
from TwitchChannelPointsMiner.classes.Webhook import Webhook

NOTIFIERS = [Apprise, Discord, Email, Gotify, Matrix, Ntfy, Pushover, Telegram, Webhook]


@pytest.mark.parametrize("notifier", NOTIFIERS, ids=lambda cls: cls.__name__)
def test_every_notifier_declares_the_send_contract(notifier):
    # The logger's notification handler and the dashboard's test button read
    # send()'s (success, reason) tuple; mypy checks the bodies against this.
    signature = inspect.signature(notifier.send)
    assert list(signature.parameters) == ["self", "message", "event"]
    assert signature.return_annotation in (SendResult, "SendResult")
    assert "events" in notifier.__slots__


@pytest.mark.parametrize("notifier", NOTIFIERS, ids=lambda cls: cls.__name__)
def test_every_notifier_matches_the_protocol(notifier):
    instance = object.__new__(notifier)
    instance.events = []

    assert isinstance(instance, Notifier)
