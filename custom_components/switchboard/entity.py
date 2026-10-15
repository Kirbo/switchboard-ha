"""Shared entity base classes (device wiring + availability)."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
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


def alert_unique_id(entry: ConfigEntry, preset_id: str, key: str) -> str:
    """The unique id of one alert-preset entity — `<entry>_alert_<preset id>_<key>`."""
    return f"{entry.entry_id}_alert_{preset_id}_{key}"


class SwitchboardAlertEntity(SwitchboardHubEntity):
    """Entity for one alert preset's queue (docs/HA.md "Alert presets"), on the hub device.

    Presets are created and deleted in the app at any time, so these entities come and go at
    runtime (`async_track_alert_entities`) instead of on an entry reload. The name follows the
    preset's name live — a rename is just the next `alert_queue_changed`.
    """

    _key: str
    _suffix: str

    def __init__(
        self, coordinator: SwitchboardCoordinator, entry: ConfigEntry, preset_id: str
    ) -> None:
        super().__init__(coordinator, entry)
        self._pid = preset_id
        self._attr_unique_id = alert_unique_id(entry, preset_id, self._key)

    @property
    def _row(self) -> dict:
        return self.coordinator.data.alerts.get(self._pid) or {}

    @property
    def name(self) -> str:
        preset = self._row.get("name") or "Alert preset"
        return f"{preset} {self._suffix}"

    @property
    def available(self) -> bool:
        return super().available and self._pid in self.coordinator.data.alerts


def async_track_alert_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: SwitchboardCoordinator,
    async_add_entities: AddEntitiesCallback,
    domain: str,
    keys: Iterable[str],
    build: Callable[[str], list[Entity]],
) -> None:
    """Keep one platform's per-preset entities in step with `coordinator.data.alerts`.

    A preset that appears (first snapshot, a resync, its first `alert_queue_changed`) gets its
    entities added; one that disappears (the deleted-preset frame, or gone from a resync's
    snapshot) gets them removed from the entity registry, which also removes the live entity —
    a deleted preset must not linger as an `unavailable` row forever. Rows left in the registry
    by a preset deleted while Home Assistant was not running are purged at setup.

    An app older than the alert presets sends no `alerts` slice: the set stays empty and this
    adds nothing.
    """
    registry = er.async_get(hass)
    keys = tuple(keys)
    prefix = f"{entry.entry_id}_alert_"
    known: set[str] = set()

    current = set(coordinator.data.alerts)
    for reg in er.async_entries_for_config_entry(registry, entry.entry_id):
        if reg.domain != domain or not reg.unique_id.startswith(prefix):
            continue
        pid, _, _key = reg.unique_id[len(prefix) :].rpartition("_")
        if pid not in current:
            registry.async_remove(reg.entity_id)

    @callback
    def _sync() -> None:
        present = set(coordinator.data.alerts)
        added = sorted(present - known)
        if added:
            known.update(added)
            async_add_entities([e for pid in added for e in build(pid)])
        for pid in known - present:
            for key in keys:
                entity_id = registry.async_get_entity_id(
                    domain, DOMAIN, alert_unique_id(entry, pid, key)
                )
                if entity_id:
                    registry.async_remove(entity_id)
        known.intersection_update(present)

    _sync()
    entry.async_on_unload(coordinator.async_add_listener(_sync))
