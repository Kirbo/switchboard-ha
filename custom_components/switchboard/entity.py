"""Shared entity base classes (device wiring + availability)."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SwitchboardCoordinator


class SwitchboardEntity(CoordinatorEntity[SwitchboardCoordinator]):
    """Base for all Switchboard entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SwitchboardCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry


class SwitchboardHubEntity(SwitchboardEntity):
    """Entity hanging off the per-instance service device (AFK, Spotify)."""

    def __init__(self, coordinator: SwitchboardCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            manufacturer="Switchboard",
            entry_type=DeviceEntryType.SERVICE,
        )


class SwitchboardObsEntity(SwitchboardEntity):
    """Entity for one OBS connection (its own device under the hub)."""

    def __init__(
        self, coordinator: SwitchboardCoordinator, entry: ConfigEntry, connection_id: str
    ) -> None:
        super().__init__(coordinator, entry)
        self._cid = connection_id
        label = coordinator.data.obs.get(connection_id, {}).get("label", connection_id)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}:{connection_id}")},
            name=label,
            manufacturer="Switchboard",
            model="OBS",
            via_device=(DOMAIN, entry.entry_id),
        )

    @property
    def available(self) -> bool:
        return super().available and self._cid in self.coordinator.data.obs


class SwitchboardTwitchEntity(SwitchboardEntity):
    """Entity for one Twitch connection (its own device under the hub)."""

    def __init__(
        self, coordinator: SwitchboardCoordinator, entry: ConfigEntry, connection_id: str
    ) -> None:
        super().__init__(coordinator, entry)
        self._cid = connection_id
        label = coordinator.connection_label(connection_id)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}:{connection_id}")},
            name=label,
            manufacturer="Switchboard",
            model="Twitch",
            via_device=(DOMAIN, entry.entry_id),
        )

    @property
    def available(self) -> bool:
        # Mirror the OBS base: a connection absent from state is unavailable, not off/unknown.
        return super().available and self._cid in self.coordinator.data.twitch
