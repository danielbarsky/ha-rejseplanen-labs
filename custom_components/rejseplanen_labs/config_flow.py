"""Config and options flow for Rejseplanen Labs.

Each config entry is an independent instance, so you can run several: e.g. one
in FOLLOW mode centred on your phone, and one in FIXED mode centred on home,
each with its own radius and fixed-stop list.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_API_KEY,
    CONF_ENABLE_POSITIONS,
    CONF_FIXED_STOPS,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_MAX_DEPARTURES,
    CONF_MAX_STOPS,
    CONF_MODE,
    CONF_ON_DEMAND_ONLY,
    CONF_RADIUS,
    CONF_SCAN_INTERVAL,
    CONF_TRACKED_ENTITY,
    DEFAULT_MAX_DEPARTURES,
    DEFAULT_MAX_STOPS,
    DEFAULT_ON_DEMAND_ONLY,
    DEFAULT_RADIUS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MODE_FIXED,
    MODE_FOLLOW,
)

# Rendered into the setup form via description_placeholders. hassfest rejects
# URLs written directly into strings.json, so it has to travel this way.
LABS_URL = "https://labs.rejseplanen.dk"

_MODE_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=[MODE_FOLLOW, MODE_FIXED],
        translation_key="mode",
        mode=selector.SelectSelectorMode.LIST,
    )
)


def _common_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Fields shared by the create flow and the options flow."""
    return vol.Schema(
        {
            vol.Required(
                CONF_TRACKED_ENTITY,
                default=defaults.get(CONF_TRACKED_ENTITY, vol.UNDEFINED),
                description={"suggested_value": defaults.get(CONF_TRACKED_ENTITY)},
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain=["person", "device_tracker"])
            ),
            vol.Optional(
                CONF_LATITUDE, default=defaults.get(CONF_LATITUDE, vol.UNDEFINED)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=-90, max=90, step="any", mode="box"
                )
            ),
            vol.Optional(
                CONF_LONGITUDE, default=defaults.get(CONF_LONGITUDE, vol.UNDEFINED)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=-180, max=180, step="any", mode="box"
                )
            ),
            vol.Required(
                CONF_RADIUS, default=defaults.get(CONF_RADIUS, DEFAULT_RADIUS)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=100, max=5000, step=50, unit_of_measurement="m", mode="slider"
                )
            ),
            vol.Required(
                CONF_MAX_STOPS, default=defaults.get(CONF_MAX_STOPS, DEFAULT_MAX_STOPS)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=1, max=25, step=1, mode="slider")
            ),
            vol.Required(
                CONF_MAX_DEPARTURES,
                default=defaults.get(CONF_MAX_DEPARTURES, DEFAULT_MAX_DEPARTURES),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=1, max=20, step=1, mode="slider")
            ),
            vol.Optional(
                CONF_FIXED_STOPS,
                default=defaults.get(CONF_FIXED_STOPS, []),
            ): selector.TextSelector(
                selector.TextSelectorConfig(multiple=True)
            ),
            vol.Required(
                CONF_ENABLE_POSITIONS,
                default=defaults.get(CONF_ENABLE_POSITIONS, False),
            ): selector.BooleanSelector(),
            vol.Required(
                CONF_SCAN_INTERVAL,
                default=defaults.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=30, max=3600, step=10, unit_of_measurement="s", mode="box"
                )
            ),
            # Ignored when on-demand only is set — then nothing polls and the
            # board updates solely via the refresh button or the service.
            vol.Required(
                CONF_ON_DEMAND_ONLY,
                default=defaults.get(CONF_ON_DEMAND_ONLY, DEFAULT_ON_DEMAND_ONLY),
            ): selector.BooleanSelector(),
        }
    )


def _validate(user_input: dict[str, Any]) -> dict[str, str]:
    """Cross-field validation shared by both flows."""
    errors: dict[str, str] = {}
    if user_input[CONF_MODE] == MODE_FOLLOW and not user_input.get(
        CONF_TRACKED_ENTITY
    ):
        errors[CONF_TRACKED_ENTITY] = "tracked_entity_required"
    if user_input[CONF_MODE] == MODE_FIXED and (
        user_input.get(CONF_LATITUDE) is None
        or user_input.get(CONF_LONGITUDE) is None
    ):
        errors["base"] = "coordinates_required"
    return errors


class RejseplanenConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial setup of an instance."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate(user_input)
            if not errors:
                mode = user_input[CONF_MODE]
                title = (
                    f"Near {user_input[CONF_TRACKED_ENTITY]}"
                    if mode == MODE_FOLLOW
                    else "Near fixed location"
                )
                return self.async_create_entry(title=title, data=user_input)

        schema = vol.Schema(
            {vol.Required(CONF_API_KEY): str, vol.Required(CONF_MODE): _MODE_SELECTOR}
        ).extend(_common_schema(user_input or {}).schema)
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders={"labs_url": LABS_URL},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> OptionsFlow:
        return RejseplanenOptionsFlow()


class RejseplanenOptionsFlow(OptionsFlow):
    """Adjust an existing instance (radius, stops, follow-entity, …)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        current = {**self.config_entry.data, **self.config_entry.options}

        if user_input is not None:
            merged = {**current, **user_input}
            errors = _validate(merged)
            if not errors:
                return self.async_create_entry(title="", data=merged)
            current = merged

        schema = vol.Schema(
            {vol.Required(CONF_MODE, default=current.get(CONF_MODE)): _MODE_SELECTOR}
        ).extend(_common_schema(current).schema)
        return self.async_show_form(
            step_id="init", data_schema=schema, errors=errors
        )
