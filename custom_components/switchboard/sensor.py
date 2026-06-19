"""Sensors: per-OBS current scene + a global Spotify playback sensor."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SPOTIFY_PAUSED, SPOTIFY_PLAYING, SPOTIFY_STOPPED
from .coordinator import SwitchboardCoordinator
from .entity import SwitchboardHubEntity, SwitchboardObsEntity, SwitchboardTwitchEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: SwitchboardCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = [
        SpotifySensor(coordinator, entry),
        AppVersionSensor(coordinator, entry),
    ]
    entities += [ObsSceneSensor(coordinator, entry, cid) for cid in coordinator.obs_ids()]
    for cid in coordinator.twitch_ids():
        entities += [
            TwitchCountSensor(coordinator, entry, cid, "viewers", "Viewers", "mdi:eye"),
            TwitchCountSensor(coordinator, entry, cid, "chatters", "Chatters", "mdi:chat"),
            TwitchCategorySensor(coordinator, entry, cid),
        ]
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
            k: now.get(k)
            for k in ("title", "artist", "album", "playlist", "url", "art_url")
            if now.get(k)
        }


class AppVersionSensor(SwitchboardHubEntity, SensorEntity):
    """The running Switchboard version (attributes carry any pending update)."""

    _attr_name = "Version"
    _attr_icon = "mdi:information-outline"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_version"

    @property
    def native_value(self) -> str | None:
        return self.coordinator.data.version or None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        upd = self.coordinator.data.update
        if not upd:
            return {"update_available": False}
        return {
            "update_available": True,
            "update_version": upd.get("version"),
            "update_ready": bool(upd.get("ready")),
        }


class TwitchCountSensor(SwitchboardTwitchEntity, SensorEntity):
    """A Twitch live count (viewers / chatters) for one account."""

    def __init__(self, coordinator, entry, connection_id, key, name, icon) -> None:
        super().__init__(coordinator, entry, connection_id)
        self._key = key
        self._attr_name = name
        self._attr_icon = icon
        self._attr_unique_id = f"{entry.entry_id}_{connection_id}_{key}"

    @property
    def native_value(self) -> int | None:
        inst = self.coordinator.data.twitch.get(self._cid)
        return inst.get(self._key) if inst else None


class TwitchCategorySensor(SwitchboardTwitchEntity, SensorEntity):
    """The current Twitch category for one account (box-art URL in attributes)."""

    _attr_name = "Category"
    _attr_icon = "mdi:gamepad-variant"

    def __init__(self, coordinator, entry, connection_id) -> None:
        super().__init__(coordinator, entry, connection_id)
        self._attr_unique_id = f"{entry.entry_id}_{connection_id}_category"

    @property
    def native_value(self) -> str | None:
        inst = self.coordinator.data.twitch.get(self._cid)
        return inst.get("category_name") if inst else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        inst = self.coordinator.data.twitch.get(self._cid) or {}
        return {
            k: inst.get(k)
            for k in ("box_art_url", "title", "started_at_ms")
            if inst.get(k) is not None
        }
