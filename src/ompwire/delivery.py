"""Where a scheduled run's report goes: one `Delivery` per gateway kind.

A scheduled turn runs without a poller, so it cannot reuse a transport's Surface: it needs the
one thing that sends a finished report to a person. `schedule_run` resolves the gateway stored on
the schedule, asks this registry for its kind's Delivery, and hands over neutral fields. Nothing
here knows a platform; each kind's implementation lives with its transport.
"""

from __future__ import annotations

import abc
from collections.abc import Callable, Iterable

from . import gateways
from .attachments import Outgoing


class Delivery(abc.ABC):
    """One finished scheduled run, sent to `target` (the destination within that gateway)."""

    @abc.abstractmethod
    async def deliver(self, target: str, *, title: str, when: str, ok: bool, word: str,
                      rows: Iterable[tuple[str, str]], body_markdown: str, error: str,
                      footer: str, files: list[Outgoing], rejected: list[str]) -> int:
        """Send the report; returns how many messages went out."""


def _telegram(gateway: gateways.Gateway) -> Delivery:
    from .tg_surface import TelegramDelivery

    return TelegramDelivery(gateways.token(gateway))


def _whatsapp(gateway: gateways.Gateway) -> Delivery:
    from .wa_outbox import WhatsAppDelivery

    return WhatsAppDelivery(gateway.name)


FACTORIES: dict[str, Callable[[gateways.Gateway], Delivery]] = {
    "telegram": _telegram,
    "whatsapp": _whatsapp,
}


def for_gateway(gateway: gateways.Gateway) -> Delivery:
    factory = FACTORIES.get(gateway.kind)
    if factory is None:
        raise gateways.GatewayError(
            f"gateway '{gateway.name}' is a {gateway.kind} bot, and no delivery exists for "
            f"that kind (have: {', '.join(sorted(FACTORIES))})")
    return factory(gateway)
