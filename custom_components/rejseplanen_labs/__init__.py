"""The Rejseplanen Labs integration."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv

from .const import (
    ATTR_CONFIG_ENTRY_ID,
    CONF_ENABLE_POSITIONS,
    DOMAIN,
    SERVICE_REFRESH,
)
from .coordinator import RejseplanenCoordinator

type RejseplanenConfigEntry = ConfigEntry[RejseplanenCoordinator]

_BASE_PLATFORMS = [Platform.SENSOR, Platform.BUTTON]
_POSITION_PLATFORMS = [Platform.DEVICE_TRACKER]

_REFRESH_SCHEMA = vol.Schema(
    {vol.Optional(ATTR_CONFIG_ENTRY_ID): vol.All(cv.ensure_list, [cv.string])}
)


def _platforms(entry: ConfigEntry) -> list[Platform]:
    opts = {**entry.data, **entry.options}
    if opts.get(CONF_ENABLE_POSITIONS):
        return [*_BASE_PLATFORMS, *_POSITION_PLATFORMS]
    return list(_BASE_PLATFORMS)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register the domain-wide on-demand refresh service."""

    async def _async_refresh(call: ServiceCall) -> None:
        """Fetch now for the named instances, or all of them."""
        wanted = call.data.get(ATTR_CONFIG_ENTRY_ID)
        entries = [
            entry
            for entry in hass.config_entries.async_entries(DOMAIN)
            if getattr(entry, "runtime_data", None) is not None
            and (wanted is None or entry.entry_id in wanted)
        ]
        for entry in entries:
            await entry.runtime_data.async_manual_refresh()

    hass.services.async_register(
        DOMAIN, SERVICE_REFRESH, _async_refresh, schema=_REFRESH_SCHEMA
    )
    return True


async def async_setup_entry(
    hass: HomeAssistant, entry: RejseplanenConfigEntry
) -> bool:
    """Set up Rejseplanen Labs from a config entry."""
    coordinator = RejseplanenCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, _platforms(entry))
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: RejseplanenConfigEntry
) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(
        entry, _platforms(entry)
    )


async def _async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when options change (e.g. radius, follow-entity, poll mode).

    This also discards the coordinator's cached stop lookup, which is what we
    want — a changed radius or centre invalidates it anyway.
    """
    await hass.config_entries.async_reload(entry.entry_id)
