"""Sensors: per-OBS scene/uptime/delay, per-Twitch counts, and the global hub sensors."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, SPOTIFY_PAUSED, SPOTIFY_PLAYING, SPOTIFY_STOPPED
from .coordinator import SwitchboardCoordinator
from .entity import SwitchboardHubEntity, SwitchboardObsEntity, SwitchboardTwitchEntity


def _as_timestamp(ms: Any) -> datetime | None:
    """Switchboard sends Unix millis (`stream_started_ms` / `started_at_ms`); HA timestamp
    sensors want an aware datetime. Anything non-numeric or non-positive reads as "not set"."""
    if not isinstance(ms, (int, float)) or ms <= 0:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: SwitchboardCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = [
        SpotifySensor(coordinator, entry),
        AppVersionSensor(coordinator, entry),
        FocusedAppSensor(coordinator, entry),
    ]
    for cid in coordinator.obs_ids():
        entities += [
            ObsSceneSensor(coordinator, entry, cid),
            ObsStreamStartedSensor(coordinator, entry, cid),
            ObsStreamDelaySensor(coordinator, entry, cid),
        ]
    for cid in coordinator.twitch_ids():
        entities += [
            TwitchCountSensor(coordinator, entry, cid, "viewers", "Viewers", "mdi:eye"),
            TwitchCountSensor(coordinator, entry, cid, "chatters", "Chatters", "mdi:chat"),
            TwitchCategorySensor(coordinator, entry, cid),
            TwitchLiveSinceSensor(coordinator, entry, cid),
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

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The instance's scene list, so templates/scripts can offer a picker. Learned from the
        `obs_scenes_changed` event (the snapshot carries no scene list), so it stays empty until
        OBS reports one."""
        inst = self.coordinator.data.obs.get(self._cid) or {}
        return {"scenes": inst.get("scenes") or []}


class ObsStreamStartedSensor(SwitchboardObsEntity, SensorEntity):
    """When this OBS instance's stream started — `None` while it isn't streaming.

    docs/HA.md: derive uptime locally from this (`now − stream_started_ms`); the app never
    broadcasts a per-second counter.
    """

    _attr_name = "Stream started"
    _attr_icon = "mdi:timer-play-outline"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator, entry, connection_id) -> None:
        super().__init__(coordinator, entry, connection_id)
        self._attr_unique_id = f"{entry.entry_id}_{connection_id}_stream_started"

    @property
    def native_value(self) -> datetime | None:
        inst = self.coordinator.data.obs.get(self._cid) or {}
        return _as_timestamp(inst.get("stream_started_ms"))


class ObsStreamDelaySensor(SwitchboardObsEntity, SensorEntity):
    """This OBS instance's configured stream delay — `0` when the delay is off."""

    _attr_name = "Stream delay"
    _attr_icon = "mdi:timer-sand"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, entry, connection_id) -> None:
        super().__init__(coordinator, entry, connection_id)
        self._attr_unique_id = f"{entry.entry_id}_{connection_id}_stream_delay"

    @property
    def native_value(self) -> int:
        inst = self.coordinator.data.obs.get(self._cid) or {}
        secs = inst.get("stream_delay_secs")
        # `null` means "delay off" on both the snapshot and `obs_delay_changed`; report it as 0
        # so the sensor stays numeric (an `unknown` would break statistics/templates).
        return int(secs) if isinstance(secs, (int, float)) else 0


class FocusedAppSensor(SwitchboardHubEntity, SensorEntity):
    """The currently-focused app id (app-detection), or None when unknown."""

    _attr_name = "Focused app"
    _attr_icon = "mdi:application"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_focused_app"

    @property
    def native_value(self) -> str | None:
        return self.coordinator.data.focused_app

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The two watch flags the contract keeps separate (`watched_focused` = a watched app is
        the foreground window; `watched_running` = one is merely running) plus the running ids."""
        data = self.coordinator.data
        return {
            "running": data.running_apps,
            "watched_focused": data.watched_focused,
            "watched_running": data.watched_running,
        }


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
            for k in (
                "title",
                "artist",
                "featuring",
                "album",
                "playlist",
                "playlist_url",
                "url",
                "art_url",
                "up_next_title",
                "up_next_artist",
                "duration_ms",
            )
            if now.get(k)
        }


class AppVersionSensor(SwitchboardHubEntity, SensorEntity):
    """The running Switchboard version (attributes carry any pending update)."""

    _attr_name = "Version"
    _attr_icon = "mdi:information-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

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
            for k in ("category_id", "box_art_url", "title", "started_at_ms")
            if inst.get(k) is not None
        }


class TwitchLiveSinceSensor(SwitchboardTwitchEntity, SensorEntity):
    """When this channel went live — `None` while it is offline.

    docs/HA.md: derive uptime from `started_at_ms` locally; the app broadcasts no counter.
    """

    _attr_name = "Live since"
    _attr_icon = "mdi:timer-play-outline"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator, entry, connection_id) -> None:
        super().__init__(coordinator, entry, connection_id)
        self._attr_unique_id = f"{entry.entry_id}_{connection_id}_live_since"

    @property
    def native_value(self) -> datetime | None:
        inst = self.coordinator.data.twitch.get(self._cid) or {}
        if not inst.get("live"):
            return None
        return _as_timestamp(inst.get("started_at_ms"))
