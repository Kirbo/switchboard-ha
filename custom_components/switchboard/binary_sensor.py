"""Binary sensors: per-OBS connected/streaming/recording/frozen, a global AFK sensor, and the
local Discord client's muted / deafened / in-voice flags."""

from __future__ import annotations

from datetime import UTC, datetime

from homeassistant.components.binary_sensor import (
    DOMAIN as BINARY_SENSOR_DOMAIN,
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import SwitchboardCoordinator
from .entity import (
    SwitchboardAlertEntity,
    SwitchboardDiscordVoiceEntity,
    SwitchboardHubEntity,
    SwitchboardObsEntity,
    SwitchboardTwitchEntity,
    async_track_alert_entities,
)

# (state key, entity name, device class)
_OBS_FLAGS = (
    ("connected", "Connected", BinarySensorDeviceClass.CONNECTIVITY),
    ("streaming", "Streaming", BinarySensorDeviceClass.RUNNING),
    ("recording", "Recording", BinarySensorDeviceClass.RUNNING),
    # OBS itself froze (render loop stopped / never answers) — on = a problem only an OBS
    # restart fixes; the one to notify your phone on.
    ("frozen", "Frozen", BinarySensorDeviceClass.PROBLEM),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: SwitchboardCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[BinarySensorEntity] = [
        SwitchboardAfkSensor(coordinator, entry),
        SwitchboardInputOverlaySensor(coordinator, entry),
        SwitchboardAppActiveSensor(coordinator, entry),
        UpdateAvailableSensor(coordinator, entry),
        NeedsReauthSensor(coordinator, entry),
        DiscordMutedSensor(coordinator, entry),
        DiscordDeafenedSensor(coordinator, entry),
        DiscordInVoiceSensor(coordinator, entry),
    ]
    for cid in coordinator.obs_ids():
        entities += [
            ObsFlagSensor(coordinator, entry, cid, key, name, dev_class)
            for key, name, dev_class in _OBS_FLAGS
        ]
    entities += [TwitchLiveSensor(coordinator, entry, cid) for cid in coordinator.twitch_ids()]
    async_add_entities(entities)
    # Per alert preset, added and removed live as presets come and go (app 2026-10-15).
    async_track_alert_entities(
        hass,
        entry,
        coordinator,
        async_add_entities,
        BINARY_SENSOR_DOMAIN,
        (AlertPausedSensor.KEY,),
        lambda pid: [AlertPausedSensor(coordinator, entry, pid)],
    )


class AlertPausedSensor(SwitchboardAlertEntity, BinarySensorEntity):
    """Whether an alert preset's queue is held by hand (`switchboard.alert_pause`, a deck key, a
    rule or the app's editor). The alert on screen finishes; the rest wait until resumed."""

    KEY = "paused"
    _key = KEY
    _suffix = "paused"
    _attr_icon = "mdi:bell-pause"

    @property
    def is_on(self) -> bool:
        return bool(self._row.get("paused"))


class DiscordMutedSensor(SwitchboardDiscordVoiceEntity, BinarySensorEntity):
    """The local Discord client's microphone mute (its own mute button) — exactly what the app
    sends, never derived from the deafen state."""

    _key = "muted"
    _attr_name = "Discord muted"
    _attr_icon = "mdi:microphone-off"

    @property
    def is_on(self) -> bool:
        return bool(self._voice.get("mute"))


class DiscordDeafenedSensor(SwitchboardDiscordVoiceEntity, BinarySensorEntity):
    """The local Discord client's deafen (its own headset button)."""

    _key = "deafened"
    _attr_name = "Discord deafened"
    _attr_icon = "mdi:headphones-off"

    @property
    def is_on(self) -> bool:
        return bool(self._voice.get("deaf"))


class DiscordInVoiceSensor(SwitchboardDiscordVoiceEntity, BinarySensorEntity):
    """Whether the local Discord client sits in a voice channel or call right now.

    Attributes: `call_kind` (`server` / `dm` / `group_dm`) and `channel_id`; `channel` and `guild`
    (the names) ONLY when the app sent them — it withholds them from a token without
    `read_events_sensitive`, since which channel the streamer is in, and on which server, says who
    they are with. The names are never invented or carried over from an earlier channel.
    """

    _key = "in_voice"
    _attr_name = "In Discord voice"
    _attr_icon = "mdi:headset"
    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY

    @property
    def is_on(self) -> bool:
        return bool(self._voice.get("in_voice"))

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        voice = self._voice
        if not voice.get("in_voice"):
            return {}
        attrs: dict[str, object] = {
            "call_kind": voice.get("call_kind"),
            "channel_id": voice.get("channel_id"),
        }
        if voice.get("channel_name"):
            attrs["channel"] = voice["channel_name"]
        if voice.get("guild_name"):
            attrs["guild"] = voice["guild_name"]
        return attrs


class ObsFlagSensor(SwitchboardObsEntity, BinarySensorEntity):
    """A boolean flag (connected/streaming/recording/frozen) of one OBS instance."""

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

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """The stable halves of `GET /api/afk`.

        `threshold_secs` = the soonest idle→AFK rule threshold, with any per-scene override
        applied (`None` = nothing will auto-set AFK). `snooze_until` = when an `afk_snooze`
        window expires, as an absolute timestamp so it can't go stale between samples. The live
        `idle_secs`/`afk_in_secs` countdown is intentionally absent — it moves every second, so
        mirroring it would rewrite this entity (and its history) once a second; poll `/api/afk`
        directly if you need a ticking countdown.
        """
        data = self.coordinator.data
        until = data.afk_snooze_until_ms
        return {
            "threshold_secs": data.afk_threshold_secs,
            "snooze_until": (
                None if until is None else datetime.fromtimestamp(until / 1000, tz=UTC).isoformat()
            ),
        }


class SwitchboardInputOverlaySensor(SwitchboardHubEntity, BinarySensorEntity):
    """Whether the input overlays (keyboard / mouse / gamepad pages) are shown on the stream.

    Off = hidden by the cutscene switch (`switchboard.set_input_overlay_visible`, a deck key, a
    rule). `auto_show_at` = when Switchboard's own auto-show timer brings them back (`None` =
    no timer armed).
    """

    _attr_name = "Input overlays visible"
    _attr_icon = "mdi:keyboard-outline"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_input_overlay_visible"

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.data.input_overlay_visible)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        at = self.coordinator.data.input_overlay_auto_show_at_ms
        return {
            "auto_show_at": (
                None if at is None else datetime.fromtimestamp(at / 1000, tz=UTC).isoformat()
            ),
        }


class SwitchboardAppActiveSensor(SwitchboardHubEntity, BinarySensorEntity):
    """Whether a watched app is currently in play (focused or running) on this machine."""

    _attr_name = "Watched app active"
    _attr_icon = "mdi:gamepad-variant"
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_watched_app_active"

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.data.watched_app_active)


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


class NeedsReauthSensor(SwitchboardHubEntity, BinarySensorEntity):
    """On while any Switchboard connection's own credential was rejected by its provider.

    `needs_reauth` on `/api/connections` (docs/HA.md) covers the app's Twitch accounts and its
    downstream Home Assistant connections. It is not "offline": the app has STOPPED retrying that
    credential and nothing reconnects until a human replaces it in Switchboard — which is exactly
    the thing worth a notification, since the failure is otherwise silent until the next stream.
    """

    _attr_name = "Connection needs re-authentication"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:key-alert"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_needs_reauth"

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.reauth_needed())

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Which connections, so a notification can name them instead of saying "something"."""
        rows = self.coordinator.reauth_needed()
        return {
            "connections": [r["label"] for r in rows],
            "integrations": sorted({r["integration"] for r in rows if r["integration"]}),
        }


class UpdateAvailableSensor(SwitchboardHubEntity, BinarySensorEntity):
    """Whether a Switchboard update is available (attributes carry the version)."""

    _attr_name = "Update available"
    _attr_device_class = BinarySensorDeviceClass.UPDATE
    _attr_icon = "mdi:package-up"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

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
