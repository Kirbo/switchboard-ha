"""Coordinator behaviour: event → state patching, and the bus re-fire."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.switchboard import coordinator as coord_mod
from custom_components.switchboard.const import (
    CONF_MIRROR_CHAT_TEXT,
    CONF_MIRROR_MODERATION_TARGETS,
    DOMAIN,
    EVENT_SWITCHBOARD,
)
from custom_components.switchboard.coordinator import (
    SwitchboardCoordinator,
    _state_from_snapshot,
)

from .conftest import FakeClient
from .test_contract import ALERT_PRESET, API_AFK, API_STATE, CID, DOCUMENTED_EVENTS

TWITCH_ID = "22222222-2222-2222-2222-222222222222"

CONNECTIONS = [
    {"id": CID, "integration": "obs", "label": "Home OBS", "is_default": True, "enabled": True},
    {"id": TWITCH_ID, "integration": "twitch", "label": "Main", "is_default": True},
    {
        "id": "33333333-3333-3333-3333-333333333333",
        "integration": "home_assistant",
        "label": "Studio HA",
        "is_default": True,
    },
]


def make_coordinator(
    hass: HomeAssistant, options: dict[str, Any] | None = None
) -> SwitchboardCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, data={}, options=options or {})
    entry.add_to_hass(hass)
    client = FakeClient(state=API_STATE, connections=list(CONNECTIONS), afk=API_AFK)
    coord = SwitchboardCoordinator(hass, entry, client)  # type: ignore[arg-type]
    coord.connections = list(CONNECTIONS)
    coord.data = _state_from_snapshot(API_STATE)
    return coord


@pytest.fixture(autouse=True)
def _no_afk_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Don't make tests wait out the resample coalescing window."""
    monkeypatch.setattr(coord_mod, "AFK_COALESCE_SECS", 0)


async def test_first_refresh_pulls_state_connections_and_afk(hass: HomeAssistant) -> None:
    coord = make_coordinator(hass)
    await coord.async_refresh()
    assert coord.last_update_success
    assert coord.data.obs[CID]["current_scene"] == "Gaming"
    assert coord.data.afk_threshold_secs == 180
    assert coord.obs_ids() == {CID}
    assert coord.twitch_ids() == {TWITCH_ID}
    assert coord.ha_ids() == {"33333333-3333-3333-3333-333333333333"}


async def test_afk_endpoint_failure_does_not_fail_the_refresh(hass: HomeAssistant) -> None:
    """The AFK numbers are attributes; losing them must not take the whole entry down."""
    coord = make_coordinator(hass)

    async def boom() -> dict[str, Any]:
        raise coord_mod.SwitchboardApiError("nope")

    coord.client.fetch_afk = boom  # type: ignore[method-assign]
    await coord.async_refresh()
    assert coord.last_update_success
    assert coord.data.afk_threshold_secs is None


@pytest.mark.parametrize("frame", DOCUMENTED_EVENTS, ids=lambda f: f["type"])
async def test_no_documented_event_crashes_the_stream(
    hass: HomeAssistant, frame: dict[str, Any]
) -> None:
    """A documented event must never raise out of `_apply` — one bad frame used to be logged
    and swallowed, which is exactly how a rename hides."""
    coord = make_coordinator(hass)
    coord._handle_frame(frame)
    await hass.async_block_till_done()


async def test_every_frame_is_refired_on_the_bus(hass: HomeAssistant) -> None:
    coord = make_coordinator(hass)
    seen: list[dict[str, Any]] = []
    hass.bus.async_listen(EVENT_SWITCHBOARD, lambda e: seen.append(dict(e.data)))
    for frame in DOCUMENTED_EVENTS:
        coord._handle_frame(frame)
    await hass.async_block_till_done()
    # Compare as a multiset: HA dispatches non-callback listeners as jobs, so delivery order
    # isn't guaranteed — what matters is that no frame is dropped on the way to the bus.
    assert sorted(f["type"] for f in seen) == sorted(f["type"] for f in DOCUMENTED_EVENTS)


CHAT_FRAME: dict[str, Any] = {
    "type": "twitch_chat_message",
    "connection_id": CID,
    "channel": "kirbownd",
    "author": "mothlamp_99",
    "text": "hello chat",
    "action": False,
    "first": False,
}


async def _bus_frames(hass: HomeAssistant, coord: SwitchboardCoordinator) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []
    hass.bus.async_listen(EVENT_SWITCHBOARD, lambda e: seen.append(dict(e.data)))
    coord._handle_frame(dict(CHAT_FRAME))
    await hass.async_block_till_done()
    return seen


async def test_chat_text_is_stripped_from_the_bus_by_default(hass: HomeAssistant) -> None:
    """SB-D-022: HA's recorder archives every non-excluded custom event for `purge_keep_days`,
    so re-firing `twitch_chat_message` verbatim builds a per-line archive of who said what on
    the HA box — the archive the app refuses to write to its own log under any setting. The
    frame still fires (chat stays countable), with `author`/`text` as null — the contract's own
    redacted shape."""
    seen = await _bus_frames(hass, make_coordinator(hass))
    assert len(seen) == 1
    assert seen[0]["type"] == "twitch_chat_message"
    assert seen[0]["author"] is None
    assert seen[0]["text"] is None
    # Everything else is untouched — a counter keyed on channel/first still works.
    assert seen[0]["channel"] == "kirbownd"
    assert seen[0]["first"] is False


async def test_chat_text_reaches_the_bus_with_the_option_on(hass: HomeAssistant) -> None:
    seen = await _bus_frames(hass, make_coordinator(hass, {CONF_MIRROR_CHAT_TEXT: True}))
    assert len(seen) == 1
    assert seen[0]["author"] == "mothlamp_99"
    assert seen[0]["text"] == "hello chat"


async def test_chat_redaction_does_not_touch_other_events(hass: HomeAssistant) -> None:
    """Only the chat event is redacted here; `twitch_chat_command` and the rest stay verbatim
    (they are scope-gated by the app, and they are what the README documents as recorded)."""
    coord = make_coordinator(hass)
    seen: list[dict[str, Any]] = []
    hass.bus.async_listen(EVENT_SWITCHBOARD, lambda e: seen.append(dict(e.data)))
    command = next(f for f in DOCUMENTED_EVENTS if f["type"] == "twitch_chat_command")
    coord._handle_frame(dict(command))
    await hass.async_block_till_done()
    assert seen == [command]


MODERATION_FRAME: dict[str, Any] = {
    "type": "twitch_moderation",
    "connection_id": CID,
    "action": "twitch_user_ban",
    "target_login": "fresh_account_42",
    "target_user_id": "123456789",
    "dry_run": True,
    "ok": False,
    "error": "fresh_account_42 is protected",
    "reason": "account 3 days old",
    "duration_secs": None,
    "rule_id": "r1",
}


async def _moderation_bus_frames(
    hass: HomeAssistant, coord: SwitchboardCoordinator
) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []
    hass.bus.async_listen(EVENT_SWITCHBOARD, lambda e: seen.append(dict(e.data)))
    coord._handle_frame(dict(MODERATION_FRAME))
    await hass.async_block_till_done()
    return seen


async def test_moderation_target_is_stripped_from_the_bus_by_default(hass: HomeAssistant) -> None:
    """The recorder would otherwise keep a per-viewer moderation record (who was banned, why) on
    the HA box for `purge_keep_days` — the record the app keeps out of its own log file. The frame
    still fires with the action/outcome, so an automation can count or react to it."""
    seen = await _moderation_bus_frames(hass, make_coordinator(hass))
    assert len(seen) == 1
    frame = seen[0]
    assert frame["type"] == "twitch_moderation"
    for field in ("target_login", "target_user_id", "reason", "error"):
        assert frame[field] is None, field
    assert frame["action"] == "twitch_user_ban"
    assert frame["dry_run"] is True
    assert frame["ok"] is False
    assert frame["rule_id"] == "r1"


async def test_moderation_target_reaches_the_bus_with_the_option_on(hass: HomeAssistant) -> None:
    seen = await _moderation_bus_frames(
        hass, make_coordinator(hass, {CONF_MIRROR_MODERATION_TARGETS: True})
    )
    assert seen == [MODERATION_FRAME]


async def test_obs_events_patch_the_instance(hass: HomeAssistant) -> None:
    coord = make_coordinator(hass)
    inst = coord.data.obs[CID]

    assert coord._apply({"type": "obs_scene_changed", "connection_id": CID, "scene": "BRB"})
    assert inst["current_scene"] == "BRB"

    assert coord._apply(
        {"type": "obs_scenes_changed", "connection_id": CID, "scenes": ["BRB", "Gaming"]}
    )
    assert inst["scenes"] == ["BRB", "Gaming"]

    assert coord._apply({"type": "obs_connection", "connection_id": CID, "connected": False})
    assert inst["connected"] is False

    assert coord._apply(
        {"type": "obs_record_state", "connection_id": CID, "active": True, "state": "started"}
    )
    assert inst["recording"] is True

    assert coord._apply({"type": "obs_delay_changed", "connection_id": CID, "secs": 90})
    assert inst["stream_delay_secs"] == 90
    # `null` = delay off.
    assert coord._apply({"type": "obs_delay_changed", "connection_id": CID, "secs": None})
    assert inst["stream_delay_secs"] is None

    assert coord._apply(
        {
            "type": "stream_delay_changed",
            "connection_id": CID,
            "label": "Home OBS",
            "enabled": True,
            "seconds": 150,
            "restarting": False,
        }
    )
    assert inst["stream_delay_secs"] == 150
    assert coord._apply(
        {
            "type": "stream_delay_changed",
            "connection_id": CID,
            "label": "Home OBS",
            "enabled": False,
            "seconds": 150,
            "restarting": False,
        }
    )
    assert inst["stream_delay_secs"] is None
    # A paired peer's OBS (`peer_id` set) is never patched onto ours, even on a matching id.
    assert not coord._apply(
        {
            "type": "stream_delay_changed",
            "connection_id": CID,
            "label": "Peer OBS",
            "enabled": True,
            "seconds": 90,
            "restarting": False,
            "peer_id": "peer-1",
        }
    )
    assert inst["stream_delay_secs"] is None
    await hass.async_block_till_done()


async def test_stream_start_stop_derives_the_start_timestamp(hass: HomeAssistant) -> None:
    coord = make_coordinator(hass)
    inst = coord.data.obs[CID]

    coord._apply(
        {"type": "obs_stream_state", "connection_id": CID, "active": False, "state": "stopped"}
    )
    assert inst["streaming"] is False
    assert inst["stream_started_ms"] is None

    coord._apply(
        {"type": "obs_stream_state", "connection_id": CID, "active": True, "state": "started"}
    )
    assert inst["streaming"] is True
    assert isinstance(inst["stream_started_ms"], int)

    started = inst["stream_started_ms"]
    # A repeat of the same edge (OBS streams `starting`→`started`) must not restart the clock.
    coord._apply(
        {"type": "obs_stream_state", "connection_id": CID, "active": True, "state": "started"}
    )
    assert inst["stream_started_ms"] == started


async def test_a_restart_keeps_the_stream_start_anchor(hass: HomeAssistant) -> None:
    """SB-00250: an automatic restart the app runs (`restart: true`, docs/HA.md field notes) is
    not a stream end — its `stopped` must not clear `stream_started_ms` and its `started` must
    not move it, or the "stream started" sensor resets mid-stream."""
    coord = make_coordinator(hass)
    inst = coord.data.obs[CID]
    inst["streaming"] = True
    inst["stream_started_ms"] = 1_700_000_000_000

    for state, active in (
        ("stopping", False),
        ("stopped", False),
        ("starting", True),
        ("started", True),
    ):
        coord._apply(
            {
                "type": "obs_stream_state",
                "connection_id": CID,
                "active": active,
                "state": state,
                "restart": True,
            }
        )
        assert inst["streaming"] is active
        assert inst["stream_started_ms"] == 1_700_000_000_000, state

    # A real stop (no `restart`, as from an older app) still ends the stream.
    coord._apply(
        {"type": "obs_stream_state", "connection_id": CID, "active": False, "state": "stopped"}
    )
    assert inst["stream_started_ms"] is None


async def test_a_restart_started_with_no_known_anchor_derives_one(hass: HomeAssistant) -> None:
    """Connected mid-restart, nothing is anchored yet: the restart's `started` still derives a
    start (the next resync replaces it with the app's authoritative one)."""
    coord = make_coordinator(hass)
    inst = coord.data.obs[CID]
    inst["streaming"] = False
    inst["stream_started_ms"] = None

    coord._apply(
        {
            "type": "obs_stream_state",
            "connection_id": CID,
            "active": True,
            "state": "started",
            "restart": True,
        }
    )
    assert isinstance(inst["stream_started_ms"], int)


async def test_obs_frozen_flips_the_instance_flag(hass: HomeAssistant) -> None:
    """`obs_frozen` (app 2026-10-17) keeps the per-instance `frozen` flag — the Frozen problem
    sensor — in step with the app, both ways."""
    coord = make_coordinator(hass)
    inst = coord.data.obs[CID]
    assert inst["frozen"] is False
    assert coord._apply({"type": "obs_frozen", "connection_id": CID, "frozen": True})
    assert inst["frozen"] is True
    assert coord._apply({"type": "obs_frozen", "connection_id": CID, "frozen": False})
    assert inst["frozen"] is False


async def test_obs_event_for_an_unknown_connection_is_ignored(hass: HomeAssistant) -> None:
    coord = make_coordinator(hass)
    assert (
        coord._apply({"type": "obs_scene_changed", "connection_id": "nope", "scene": "X"}) is False
    )


async def test_twitch_events_patch_the_account(hass: HomeAssistant) -> None:
    coord = make_coordinator(hass)
    assert coord._apply(
        {
            "type": "twitch_category_updated",
            "connection_id": TWITCH_ID,
            "game_id": "32399",
            "game_name": "Counter-Strike",
        }
    )
    assert coord.data.twitch[TWITCH_ID]["category_name"] == "Counter-Strike"
    assert coord.data.twitch[TWITCH_ID]["category_id"] == "32399"

    assert coord._apply(
        {
            "type": "twitch_chatters_updated",
            "connection_id": TWITCH_ID,
            "watching": 9,
            "chatters": 3,
        }
    )
    assert coord.data.twitch[TWITCH_ID]["viewers"] == 9
    assert coord.data.twitch[TWITCH_ID]["chatters"] == 3

    assert coord._apply({"type": "twitch_stream_status", "connection_id": TWITCH_ID, "live": False})
    assert coord.data.twitch[TWITCH_ID]["live"] is False

    # Audience totals ride their own event (tracker cadence, on change) — the same numbers the
    # snapshot seeds as `followers`/`subs`, so the Followers/Subscribers sensors stay live.
    assert coord.data.twitch[TWITCH_ID]["followers"] == 1234  # seeded from API_STATE
    assert coord._apply(
        {
            "type": "twitch_audience_totals",
            "connection_id": TWITCH_ID,
            "followers": 1300,
            "subs": 60,
        }
    )
    assert coord.data.twitch[TWITCH_ID]["followers"] == 1300
    assert coord.data.twitch[TWITCH_ID]["subs"] == 60


async def test_variable_changed_patches_the_variables_map(hass: HomeAssistant) -> None:
    coord = make_coordinator(hass)
    assert coord.data.variables == {"deaths": "3", "mode": "gaming"}  # seeded from /api/state
    assert coord._apply({"type": "variable_changed", "name": "deaths", "value": "4"})
    assert coord.data.variables == {"deaths": "4", "mode": "gaming"}
    # A new name is added, not dropped — the snapshot map is a seed, the events keep it current.
    assert coord._apply({"type": "variable_changed", "name": "raids", "value": "1"})
    assert coord.data.variables["raids"] == "1"
    # A frame without a usable name changes nothing (and does not raise).
    assert not coord._apply({"type": "variable_changed", "value": "9"})


async def test_a_deleted_variable_leaves_the_variables_map(hass: HomeAssistant) -> None:
    coord = make_coordinator(hass)
    assert coord._apply(
        {"type": "variable_changed", "name": "deaths", "value": "", "deleted": True}
    )
    assert coord.data.variables == {"mode": "gaming"}
    # Deleting a name the mirror does not hold changes nothing.
    assert not coord._apply(
        {"type": "variable_changed", "name": "deaths", "value": "", "deleted": True}
    )
    # `deleted: false` (and an absent flag) is an ordinary set — even to "".
    assert coord._apply(
        {"type": "variable_changed", "name": "deaths", "value": "", "deleted": False}
    )
    assert coord.data.variables == {"deaths": "", "mode": "gaming"}


async def test_app_detect_keeps_the_two_flags_separate(hass: HomeAssistant) -> None:
    coord = make_coordinator(hass)
    assert coord._apply(
        {
            "type": "app_detect_changed",
            "focused": None,
            "watched_focused": False,
            "running": ["steam_app_599140"],
            "watched_running": True,
        }
    )
    assert coord.data.focused_app is None
    assert coord.data.watched_focused is False
    assert coord.data.watched_running is True
    assert coord.data.watched_app_active is True  # running still counts as "in play"


async def test_machine_state_event_drives_afk(hass: HomeAssistant) -> None:
    coord = make_coordinator(hass)
    assert coord._apply({"type": "machine_state_changed", "state": "afk", "source": "External API"})
    assert coord.data.afk is True
    assert coord._apply({"type": "machine_state_changed", "state": "active"})
    assert coord.data.afk is False
    await hass.async_block_till_done()


async def test_spotify_position_drift_does_not_rewrite_the_sensor(hass: HomeAssistant) -> None:
    """`spotify_now_playing` is re-emitted on position drift — only real changes may signal."""
    coord = make_coordinator(hass)
    now = {"playing": True, "title": "T", "artist": "A", "position_ms": 1000}
    assert coord._apply({"type": "spotify_now_playing", "now": now})
    assert (
        coord._apply({"type": "spotify_now_playing", "now": {**now, "position_ms": 6000}}) is False
    )
    assert coord._apply({"type": "spotify_now_playing", "now": {**now, "title": "T2"}}) is True


async def test_spotify_gate_transitions(hass: HomeAssistant) -> None:
    coord = make_coordinator(hass)
    coord._apply({"type": "spotify_playback_paused", "now": {"playing": False, "title": "T"}})
    assert coord.data.spotify == "paused"
    coord._apply({"type": "spotify_playback_started", "now": {"playing": True, "title": "T"}})
    assert coord.data.spotify == "playing"
    # No payload on this one at all.
    coord._apply({"type": "spotify_playback_stopped"})
    assert coord.data.spotify == "stopped"
    assert coord.data.spotify_now is None
    # A `now: null` frame means stopped too, not paused.
    coord._apply({"type": "spotify_now_playing", "now": None})
    assert coord.data.spotify == "stopped"


async def test_afk_resample_is_scheduled_and_applied(hass: HomeAssistant) -> None:
    coord = make_coordinator(hass)
    coord.client.afk = {**API_AFK, "threshold_secs": 60}  # type: ignore[attr-defined]
    coord._apply({"type": "obs_scene_changed", "connection_id": CID, "scene": "BRB"})
    await hass.async_block_till_done()
    assert coord.data.afk_threshold_secs == 60


async def test_resolve_connection_id_by_label_and_id(hass: HomeAssistant) -> None:
    coord = make_coordinator(hass)
    assert coord.resolve_connection_id("Home OBS") == CID
    assert coord.resolve_connection_id(CID) == CID
    assert coord.resolve_connection_id("nope") is None
    assert coord.default_id("home_assistant") == "33333333-3333-3333-3333-333333333333"


async def test_ambiguous_label_raises(hass: HomeAssistant) -> None:
    coord = make_coordinator(hass)
    coord.connections = [
        {"id": "a", "integration": "obs", "label": "Same"},
        {"id": "b", "integration": "obs", "label": "Same"},
    ]
    with pytest.raises(ValueError, match="ambiguous"):
        coord.resolve_connection_id("Same")


async def test_connections_without_optional_keys_do_not_crash(hass: HomeAssistant) -> None:
    """`/api/connections` is additive; a row missing a key we read must not KeyError."""
    coord = make_coordinator(hass)
    coord.connections = [{"id": "a"}]
    assert coord.obs_ids() == set()
    assert coord.resolve_connection_id("whatever") is None
    assert coord.connection_label("a") == "a"


async def test_reauth_needed_lists_only_rejected_credentials(hass: HomeAssistant) -> None:
    """`needs_reauth` (docs/HA.md `/api/connections`) is "a human must replace this credential",
    not "currently offline" — a disabled or merely disconnected row must not appear."""
    coord = make_coordinator(hass)
    assert coord.reauth_needed() == []
    coord.connections = [
        {"id": "a", "integration": "twitch", "label": "Main", "needs_reauth": True},
        {"id": "b", "integration": "obs", "label": "Home OBS", "enabled": False},
        {"id": "c", "integration": "home_assistant", "label": "Studio HA", "needs_reauth": False},
    ]
    assert coord.reauth_needed() == [
        {"id": "a", "label": "Main", "integration": "twitch"},
    ]


async def test_a_needs_reauth_flip_pushes_to_entities(hass: HomeAssistant) -> None:
    """`connections_changed` with the SAME entity shape used to update nothing — the entity
    reading `needs_reauth` would have sat stale until the next reconnect resync."""
    coord = make_coordinator(hass)
    updates = 0

    def _listener() -> None:
        nonlocal updates
        updates += 1

    coord.async_add_listener(_listener)
    coord.client.connections = [  # type: ignore[attr-defined]
        {**c, "needs_reauth": c["integration"] == "twitch"} for c in CONNECTIONS
    ]
    coord._apply({"type": "connections_changed"})
    await hass.async_block_till_done()
    assert [r["label"] for r in coord.reauth_needed()] == ["Main"]
    assert updates >= 1


async def test_input_overlay_visibility_event_drives_the_flag(hass: HomeAssistant) -> None:
    """The cutscene switch: the event carries the flag + the app's auto-show time. The contract
    snapshot seeds it hidden-with-a-timer; an older app's snapshot (no field) reads as shown."""
    coord = make_coordinator(hass)
    assert coord.data.input_overlay_visible is False
    assert coord.data.input_overlay_auto_show_at_ms == 1700000300000
    assert coord._apply(
        {"type": "input_overlay_visibility", "visible": True, "auto_show_at_ms": None}
    )
    assert coord.data.input_overlay_visible is True
    assert coord.data.input_overlay_auto_show_at_ms is None
    assert coord._apply(
        {
            "type": "input_overlay_visibility",
            "visible": False,
            "auto_show_at_ms": 1700000300000,
            "source": "OpenDeck",
        }
    )
    assert coord.data.input_overlay_visible is False
    assert coord.data.input_overlay_auto_show_at_ms == 1700000300000
    assert _state_from_snapshot({}).input_overlay_visible is True


def _alert_frame(**fields: Any) -> dict[str, Any]:
    return {
        "type": "alert_queue_changed",
        "preset_id": ALERT_PRESET,
        "name": "Main alerts",
        "paused": False,
        "queued": 3,
        "dropped": 0,
        "merged": 14,
        "showing": "twitch_raid",
        **fields,
    }


async def test_alert_queue_changed_patches_the_alerts_slice(hass: HomeAssistant) -> None:
    """docs/HA.md "Alert presets": seeded from /api/state, kept live by `alert_queue_changed`."""
    coord = make_coordinator(hass)
    assert coord.data.alerts[ALERT_PRESET]["queued"] == 3  # seeded from /api/state
    # The same values again change nothing (no entity rewrite, no recorder row).
    assert not coord._apply(_alert_frame())
    assert coord._apply(_alert_frame(queued=0, paused=True, showing=None, dropped=2))
    assert coord.data.alerts[ALERT_PRESET] == {
        "name": "Main alerts",
        "queued": 0,
        "paused": True,
        "showing": None,
        "dropped": 2,
        "merged": 14,
    }
    # A preset the snapshot did not know (created since) is added.
    other = "55555555-5555-5555-5555-555555555555"
    assert coord._apply(_alert_frame(preset_id=other, name="Second", queued=1))
    assert coord.data.alerts[other]["name"] == "Second"
    # A frame without a usable preset id changes nothing (and does not raise).
    assert not coord._apply({"type": "alert_queue_changed", "queued": 1})


async def test_alert_queue_frame_without_a_name_keeps_the_known_name(
    hass: HomeAssistant,
) -> None:
    coord = make_coordinator(hass)
    frame = _alert_frame(queued=5)
    del frame["name"]
    assert coord._apply(frame)
    assert coord.data.alerts[ALERT_PRESET]["name"] == "Main alerts"


async def test_the_deleted_preset_frame_drops_the_row(hass: HomeAssistant) -> None:
    """A deleted preset sends one last all-zero frame with an EMPTY name, then is gone."""
    coord = make_coordinator(hass)
    deleted = _alert_frame(name="", queued=0, merged=0, showing=None)
    assert coord._apply(deleted)
    assert ALERT_PRESET not in coord.data.alerts
    # The same frame again (or for a preset we never had) is a no-op.
    assert not coord._apply(deleted)


async def test_an_idle_preset_is_not_mistaken_for_a_deleted_one(hass: HomeAssistant) -> None:
    """All-zero counters with the preset's real name is just an idle queue."""
    coord = make_coordinator(hass)
    assert coord._apply(_alert_frame(queued=0, merged=0, showing=None))
    assert ALERT_PRESET in coord.data.alerts


def _status_entry(key: str, state: str, **fields: Any) -> dict[str, Any]:
    integration, component, ident = key.split("/", 2)
    return {
        "key": key,
        "integration": integration,
        "component": component,
        "connection_id": None,
        "subject": ident,
        "label": None,
        "state": state,
        "since_ms": 1760800000000,
        "last_error": None,
        "last_error_ms": None,
        "drops": 0,
        "reconnects": 0,
        "crashes": 0,
        "queued": None,
        **fields,
    }


async def test_connection_status_frames_patch_the_rows(hass: HomeAssistant) -> None:
    """docs/HA.md "Connection status": seeded from /api/state, a changed row replaced by key, an
    identical frame a no-op, a removal drops the row; a malformed frame changes nothing."""
    coord = make_coordinator(hass)
    assert coord.data.connection_status is not None
    seeded = len(coord.data.connection_status)
    key = "system/loop/insights"
    frame = {"type": "connection_status_changed", "entry": _status_entry(key, "crashed", crashes=1)}
    assert coord._apply(frame)
    assert coord.data.connection_status[key]["crashes"] == 1
    assert not coord._apply(frame)
    assert coord._apply(
        {
            "type": "connection_status_changed",
            "entry": _status_entry(key, "connected", crashes=1, reconnects=1),
        }
    )
    assert coord.data.connection_status[key]["state"] == "connected"
    assert not coord._apply({"type": "connection_status_changed", "entry": {"state": "down"}})
    assert coord._apply({"type": "connection_status_removed", "key": key})
    assert len(coord.data.connection_status) == seeded
    assert not coord._apply({"type": "connection_status_removed", "key": key})


async def test_an_unknown_status_state_never_reads_as_fine(hass: HomeAssistant) -> None:
    coord = make_coordinator(hass)
    key = "obs/connection/future"
    assert coord._apply(
        {"type": "connection_status_changed", "entry": _status_entry(key, "from_the_future")}
    )
    assert coord.data.connection_status[key]["state"] == "down"


async def test_a_status_frame_creates_the_rows_against_an_older_snapshot(
    hass: HomeAssistant,
) -> None:
    coord = make_coordinator(hass)
    coord.data.connection_status = None
    assert not coord._apply({"type": "connection_status_removed", "key": "x/y/z"})
    assert coord._apply(
        {"type": "connection_status_changed", "entry": _status_entry("x/y/z", "down")}
    )
    assert coord.data.connection_status == {"x/y/z": coord.data.connection_status["x/y/z"]}


# --- docs/HA.md "Discord voice" (app 2026-10-21) ---------------------------------------------

DISCORD_ROW = "discord/rpc/local"


def _discord_row(state: str) -> dict[str, Any]:
    return {"type": "connection_status_changed", "entry": _status_entry(DISCORD_ROW, state)}


def _channel(channel_id: str | None, **fields: Any) -> dict[str, Any]:
    return {
        "type": "discord_voice_channel_changed",
        "channel_id": channel_id,
        "channel_name": None,
        "guild_id": None,
        "guild_name": None,
        "call_kind": None,
        "previous_channel_id": None,
        "previous_channel_name": None,
        **fields,
    }


def _settings(mute: bool, deaf: bool, mode: str = "voice_activity") -> dict[str, Any]:
    return {
        "type": "discord_voice_settings_changed",
        "mute": mute,
        "deaf": deaf,
        "input_mode": mode,
        "previous_mute": not mute,
        "previous_deaf": deaf,
        "previous_input_mode": mode,
    }


async def test_discord_channel_frames_patch_the_voice_slice(hass: HomeAssistant) -> None:
    """Join → switch to a call → leave: each frame replaces the channel fields; a leave clears
    every one of them, so no stale name or kind survives it."""
    coord = make_coordinator(hass)
    assert coord.data.discord_voice is not None
    assert coord._apply(
        _channel(
            "9", channel_name="Raid planning", guild_name="Guild", call_kind="server", guild_id="1"
        )
    )
    dv = coord.data.discord_voice
    assert dv["in_voice"] is True
    assert dv["channel_id"] == "9"
    assert dv["channel_name"] == "Raid planning"
    assert dv["guild_name"] == "Guild"
    assert coord._apply(_channel("10", call_kind="dm", previous_channel_id="9"))
    dv = coord.data.discord_voice
    assert dv["call_kind"] == "dm"
    assert dv["channel_name"] is None, "a call carries no name here — never the old channel's"
    assert dv["guild_name"] is None
    assert coord._apply(_channel(None, previous_channel_id="10"))
    dv = coord.data.discord_voice
    assert dv["in_voice"] is False
    assert dv["channel_id"] is None
    assert dv["call_kind"] is None
    # Mute/deafen are untouched by channel moves.
    assert dv["mute"] is False
    assert not coord._apply(_channel(None, previous_channel_id="10")), "same state = no update"


async def test_discord_settings_frames_patch_mute_deaf_and_mode(hass: HomeAssistant) -> None:
    coord = make_coordinator(hass)
    assert coord._apply(_settings(mute=True, deaf=False))
    assert coord.data.discord_voice["mute"] is True
    assert coord._apply(_settings(mute=True, deaf=True, mode="push_to_talk"))
    dv = coord.data.discord_voice
    assert dv["deaf"] is True
    assert dv["input_mode"] == "push_to_talk"
    # The channel is untouched by a settings change.
    assert dv["channel_id"] == "100000000000000001"
    assert not coord._apply(_settings(mute=True, deaf=True, mode="push_to_talk"))


async def test_discord_frames_before_the_slice_is_seeded_change_nothing(
    hass: HomeAssistant,
) -> None:
    """No live session in the snapshot (`discord_voice: null`): a frame cannot build a whole state
    (it does not carry the other half), so it waits for the re-read the status row triggers."""
    coord = make_coordinator(hass)
    coord.data.discord_voice = None
    assert not coord._apply(_settings(mute=True, deaf=False))
    assert not coord._apply(_channel("9", call_kind="server"))
    assert coord.data.discord_voice is None


async def test_the_discord_row_leaving_connected_makes_the_state_unknown(
    hass: HomeAssistant,
) -> None:
    coord = make_coordinator(hass)
    assert coord._apply(_discord_row("connected"))
    assert coord._reseed_requested, "first sight of `connected` asks for the baseline"
    coord._reseed_requested = False
    assert coord.data.discord_voice is not None
    assert coord._apply(_discord_row("disabled"))  # Discord quit
    assert coord.data.discord_voice is None
    assert not coord._reseed_requested


async def test_the_discord_row_turning_connected_asks_for_a_reseed_once(
    hass: HomeAssistant,
) -> None:
    coord = make_coordinator(hass)
    assert coord._apply(_discord_row("connecting"))
    assert not coord._reseed_requested
    assert coord._apply(_discord_row("connected"))
    assert coord._reseed_requested
    coord._reseed_requested = False
    # A later change to the same, still-connected row (a counter) is no new session.
    frame = _discord_row("connected")
    frame["entry"]["reconnects"] = 1
    assert coord._apply(frame)
    assert not coord._reseed_requested


async def test_other_rows_never_touch_the_discord_slice(hass: HomeAssistant) -> None:
    coord = make_coordinator(hass)
    before = dict(coord.data.discord_voice)
    assert coord._apply(
        {"type": "connection_status_changed", "entry": _status_entry("obs/connection/x", "down")}
    )
    assert coord.data.discord_voice == before
    assert not coord._reseed_requested


async def test_a_removed_discord_row_makes_the_state_unknown(hass: HomeAssistant) -> None:
    coord = make_coordinator(hass)
    assert coord._apply(_discord_row("connected"))
    assert coord._apply({"type": "connection_status_removed", "key": DISCORD_ROW})
    assert coord.data.discord_voice is None
