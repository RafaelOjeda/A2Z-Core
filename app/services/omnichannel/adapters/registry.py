"""Channel adapter registry (CLAUDE.md §5.2, §7).

The generic webhook route (§5.6) and the shared inbound SQS queue (§5.6) both
dispatch through ``get_adapter`` on their ``channel_type`` message attribute.
Adding a channel means adding one adapter file and one line here -- no other
code or infra changes.
"""

from __future__ import annotations

from app.services.omnichannel.adapters.base import ChannelAdapter
from app.services.omnichannel.adapters.email import EmailAdapter
from app.services.omnichannel.adapters.whatsapp import WhatsAppAdapter
from app.services.omnichannel.exceptions import ChannelAdapterError

_REGISTRY: dict[str, ChannelAdapter] = {
    "email": EmailAdapter(),
    "whatsapp": WhatsAppAdapter(),
}


def get_adapter(channel_type: str) -> ChannelAdapter:
    """Look up the adapter for a channel_type.

    Raises:
        ChannelAdapterError: No adapter is registered for ``channel_type``.
    """
    adapter = _REGISTRY.get(channel_type)
    if adapter is None:
        raise ChannelAdapterError(f"No adapter registered for channel_type={channel_type!r}")
    return adapter
