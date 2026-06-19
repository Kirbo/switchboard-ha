"""Binary sensors: per-OBS connected/streaming/recording + a global AFK sensor."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import SwitchboardCoordinator
from .entity import SwitchboardHubEntity, SwitchboardObsEntity, SwitchboardTwitchEntity

# (state key, entity name, device class)
_OBS_FLAGS = (
    ("connected", "Connected", BinarySensorDeviceClass.CONNECTIVITY),
    ("streaming", "Streaming", BinarySensorDeviceClass.RUNNING),
    ("recording", "Recording", BinarySensorDeviceClass.RUNNING),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: SwitchboardCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[BinarySensorEntity] = [
        SwitchboardAfkSensor(coordinator, entry),
        UpdateAvailableSensor(coordinator, entry),
    ]
    for cid in coordinator.obs_ids():
        entities += [
            ObsFlagSensor(coordinator, entry, cid, key, name, dev_class)
            for key, name, dev_class in _OBS_FLAGS
        ]
    entities += [TwitchLiveSensor(coordinator, entry, cid) for cid in coordinator.twitch_ids()]
    async_add_entities(entities)


class ObsFlagSensor(SwitchboardObsEntity, BinarySensorEntity):
    """A boolean flag (connected/streaming/recording) of one OBS instance."""

    def __init__(self, coordinator, entry, connection_id, key, name, device_class) -> None:
        super().__init__(coordinator, entry, connection_id)
        self._key = key
        self._attr_name = name
        self._attr_device_class = device_class
        self._attr_unique_id = f"{entry.entry_id}_{connection_id}_{key}"

    @property
    def is_on(self) -> bool:
        inst = self.coordinator.data.obs.get(self._cid)
        return bool(inst and inst.get(self._key))


class SwitchboardAfkSensor(SwitchboardHubEntity, BinarySensorEntity):
    """Whether the machine is AFK (idle threshold crossed)."""

    _attr_name = "AFK"
    _attr_icon = "mdi:account-clock"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_afk"

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.data.afk)


class TwitchLiveSensor(SwitchboardTwitchEntity, BinarySensorEntity):
    """Whether a Twitch channel is currently live."""

    _attr_name = "Live"
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, coordinator, entry, connection_id) -> None:
        super().__init__(coordinator, entry, connection_id)
        self._attr_unique_id = f"{entry.entry_id}_{connection_id}_live"

    @property
    def is_on(self) -> bool:
        inst = self.coordinator.data.twitch.get(self._cid)
        return bool(inst and inst.get("live"))


class UpdateAvailableSensor(SwitchboardHubEntity, BinarySensorEntity):
    """Whether a Switchboard update is available (attributes carry the version)."""

    _attr_name = "Update available"
    _attr_device_class = BinarySensorDeviceClass.UPDATE
    _attr_icon = "mdi:package-up"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_update_available"

    @property
    def is_on(self) -> bool:
        return self.coordinator.data.update is not None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        upd = self.coordinator.data.update
        if not upd:
            return {}
        return {"version": upd.get("version"), "ready": bool(upd.get("ready"))}
