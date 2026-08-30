"""A refresh button, for when you're en route and want the truth right now.

Pressing this fetches immediately, regardless of the update interval (and it's
the whole point of on-demand mode, where there is no interval). Presses inside
MANUAL_REFRESH_COOLDOWN collapse into one fetch, so impatient tapping at the
bus stop can't drain the quota.
"""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import RejseplanenConfigEntry
from .const import ATTR_ATTRIBUTION, DOMAIN
from .coordinator import RejseplanenCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RejseplanenConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the manual refresh button."""
    async_add_entities([RejseplanenRefreshButton(entry.runtime_data, entry)])


class RejseplanenRefreshButton(ButtonEntity):
    """Fetch departures now."""

    _attr_has_entity_name = True
    _attr_translation_key = "refresh"
    _attr_icon = "mdi:refresh"
    _attr_attribution = ATTR_ATTRIBUTION

    def __init__(
        self, coordinator: RejseplanenCoordinator, entry: RejseplanenConfigEntry
    ) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_refresh"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": entry.title,
            "manufacturer": "Rejseplanen Labs",
        }

    async def async_press(self) -> None:
        await self._coordinator.async_manual_refresh()
