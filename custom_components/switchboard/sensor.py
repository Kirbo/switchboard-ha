"""Sensors: per-OBS current scene + a global Spotify playback sensor."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SPOTIFY_PAUSED, SPOTIFY_PLAYING, SPOTIFY_STOPPED
from .coordinator import SwitchboardCoordinator
from .entity import SwitchboardHubEntity, SwitchboardObsEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: SwitchboardCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = [SpotifySensor(coordinator, entry)]
    entities += [ObsSceneSensor(coordinator, entry, cid) for cid in coordinator.obs_ids()]
    async_add_entities(entities)


class ObsSceneSensor(SwitchboardObsEntity, SensorEntity):
    """The current program scene of one OBS instance."""

    _attr_name = "Scene"
    _attr_icon = "mdi:movie-open"

    def __init__(self, coordinator, entry, connection_id) -> None:
        super().__init__(coordinator, entry, connection_id)
        self._attr_unique_id = f"{entry.entry_id}_{connection_id}_scene"

    @property
    def native_value(self) -> str | None:
        inst = self.coordinator.data.obs.get(self._cid)
        return inst.get("current_scene") if inst else None


class SpotifySensor(SwitchboardHubEntity, SensorEntity):
    """Spotify playback gate (playing/paused/stopped) + now-playing attributes."""

    _attr_name = "Spotify"
    _attr_icon = "mdi:spotify"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [SPOTIFY_PLAYING, SPOTIFY_PAUSED, SPOTIFY_STOPPED]

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_spotify"

    @property
    def native_value(self) -> str:
        return self.coordinator.data.spotify

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        now = self.coordinator.data.spotify_now
        if not now:
            return {}
        return {
            k: now.get(k) for k in ("title", "artist", "album", "playlist", "url") if now.get(k)
        }
