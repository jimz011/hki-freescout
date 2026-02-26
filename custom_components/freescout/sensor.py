"""Sensor platform for FreeScout."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    SENSOR_MY_TICKETS,
    SENSOR_NEW,
    SENSOR_OPEN,
    SENSOR_PENDING,
    SENSOR_SNOOZED,
    SENSOR_UNASSIGNED,
)
from .coordinator import FreescoutCoordinator


@dataclass(frozen=True)
class FreescoutSensorDescription(SensorEntityDescription):
    """Extend the base description with a flag for agent-only sensors."""

    requires_agent: bool = False


SENSOR_DESCRIPTIONS: tuple[FreescoutSensorDescription, ...] = (
    FreescoutSensorDescription(
        key=SENSOR_OPEN,
        name="Open Tickets",
        icon="mdi:ticket-outline",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="tickets",
    ),
    FreescoutSensorDescription(
        key=SENSOR_UNASSIGNED,
        name="Unassigned Tickets",
        icon="mdi:ticket-account",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="tickets",
    ),
    FreescoutSensorDescription(
        key=SENSOR_PENDING,
        name="Pending Tickets",
        icon="mdi:clock-outline",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="tickets",
    ),
    FreescoutSensorDescription(
        key=SENSOR_SNOOZED,
        name="Snoozed Tickets",
        icon="mdi:alarm-snooze",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="tickets",
    ),
    FreescoutSensorDescription(
        key=SENSOR_NEW,
        name="New Tickets",
        icon="mdi:ticket-confirmation-outline",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="tickets",
    ),
    FreescoutSensorDescription(
        key=SENSOR_MY_TICKETS,
        name="My Assigned Tickets",
        icon="mdi:account-check",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="tickets",
        requires_agent=True,
    ),
)


def _device_info(coordinator: FreescoutCoordinator, entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="FreeScout",
        manufacturer="FreeScout",
        entry_type=DeviceEntryType.SERVICE,
        configuration_url=coordinator.base_url,
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: FreescoutCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities: list[SensorEntity] = [
        FreescoutSensor(coordinator, entry, desc)
        for desc in SENSOR_DESCRIPTIONS
        if not desc.requires_agent or coordinator.agent_id
    ]

    # Dynamic sensors for each custom folder (team queues like ECU-Repair, Dashboard Repair, …)
    for folder in coordinator.custom_folders:
        entities.append(
            FreescoutFolderSensor(coordinator, entry, folder["name"], folder["key"])
        )

    async_add_entities(entities)


class FreescoutSensor(CoordinatorEntity[FreescoutCoordinator], SensorEntity):
    """A single FreeScout metric exposed as a HA sensor."""

    entity_description: FreescoutSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: FreescoutCoordinator,
        entry: ConfigEntry,
        description: FreescoutSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = _device_info(coordinator, entry)

    @property
    def native_value(self) -> int | None:
        return self.coordinator.data.get(self.entity_description.key)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"freescout_url": self.coordinator.base_url}


class FreescoutFolderSensor(CoordinatorEntity[FreescoutCoordinator], SensorEntity):
    """A sensor for a single FreeScout custom folder (team queue)."""

    _attr_has_entity_name = True
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "tickets"
    _attr_icon = "mdi:folder-open"

    def __init__(
        self,
        coordinator: FreescoutCoordinator,
        entry: ConfigEntry,
        folder_name: str,
        folder_key: str,
    ) -> None:
        super().__init__(coordinator)
        self._folder_key = folder_key
        self._attr_name = folder_name
        self._attr_unique_id = f"{entry.entry_id}_{folder_key}"
        self._attr_device_info = _device_info(coordinator, entry)

    @property
    def native_value(self) -> int | None:
        return self.coordinator.data.get(self._folder_key)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"freescout_url": self.coordinator.base_url}
