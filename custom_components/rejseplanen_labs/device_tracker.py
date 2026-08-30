"""Live vehicle positions as device_trackers (Buster-style live map).

Vehicles appear and disappear as they enter/leave the search area, so trackers
are created dynamically as new journey ids show up in the coordinator data. A
tracker for a vehicle that's gone reports unavailable rather than being torn
down (HA doesn't cleanly remove entities mid-session).

NOTE: this platform only produces entities once api.journey_positions() is
implemented against your Labs key — until then the coordinator returns an
empty vehicle list and no trackers are created.
"""

from __future__ import annotations

from homeassistant.components.device_tracker import SourceType, TrackerEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import RejseplanenConfigEntry
from .api import VehiclePosition
from .const import ATTR_ATTRIBUTION, DATA_VEHICLES, DOMAIN
from .coordinator import RejseplanenCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: RejseplanenConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create trackers dynamically as vehicles appear."""
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _sync_vehicles() -> None:
        vehicles: list[VehiclePosition] = coordinator.data.get(DATA_VEHICLES, [])
        new = [v for v in vehicles if v.journey_id not in known]
        if not new:
            return
        known.update(v.journey_id for v in new)
        async_add_entities(
            RejseplanenVehicleTracker(coordinator, entry, v.journey_id) for v in new
        )

    _sync_vehicles()
    entry.async_on_unload(coordinator.async_add_listener(_sync_vehicles))


class RejseplanenVehicleTracker(
    CoordinatorEntity[RejseplanenCoordinator], TrackerEntity
):
    """A single live vehicle position."""

    _attr_has_entity_name = True
    _attr_attribution = ATTR_ATTRIBUTION
    _attr_icon = "mdi:bus-marker"

    def __init__(
        self,
        coordinator: RejseplanenCoordinator,
        entry: RejseplanenConfigEntry,
        journey_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._journey_id = journey_id
        self._attr_unique_id = f"{entry.entry_id}_vehicle_{journey_id}"

    @property
    def _vehicle(self) -> VehiclePosition | None:
        for vehicle in self.coordinator.data.get(DATA_VEHICLES, []):
            if vehicle.journey_id == self._journey_id:
                return vehicle
        return None

    @property
    def available(self) -> bool:
        return super().available and self._vehicle is not None

    @property
    def name(self) -> str | None:
        vehicle = self._vehicle
        return vehicle.name if vehicle else self._journey_id

    @property
    def source_type(self) -> SourceType:
        return SourceType.GPS

    @property
    def latitude(self) -> float | None:
        vehicle = self._vehicle
        return vehicle.latitude if vehicle else None

    @property
    def longitude(self) -> float | None:
        vehicle = self._vehicle
        return vehicle.longitude if vehicle else None

    @property
    def extra_state_attributes(self) -> dict:
        vehicle = self._vehicle
        if not vehicle:
            return {}
        return {"type": vehicle.type, "direction": vehicle.direction}
