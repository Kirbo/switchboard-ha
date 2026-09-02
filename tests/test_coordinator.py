"""Coordinator behaviour: event → state patching, and the bus re-fire."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.switchboard import coordinator as coord_mod
from custom_components.switchboard.const import DOMAIN, EVENT_SWITCHBOARD
from custom_components.switchboard.coordinator import (
    SwitchboardCoordinator,
    _state_from_snapshot,
)

from .conftest import FakeClient
from .test_contract import API_AFK, API_STATE, CID, DOCUMENTED_EVENTS

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


def make_coordinator(hass: HomeAssistant) -> SwitchboardCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, data={})
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
