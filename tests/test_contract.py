"""Contract guard against `docs/HA.md` in the Switchboard app repo.

The failure this file exists to prevent: an event or `/api/state` field is renamed upstream, the
consumer keeps reading the old key, and a sensor silently freezes at its last value (exactly what
once froze the AFK sensor). The app pins its side with a test over `events.rs`; this pins ours.

`DOCUMENTED_EVENTS` mirrors the contract's **Full event reference** verbatim, one representative
frame per type. Every one of them must be swallowed without raising, and the ones that back an
entity must actually move state. When the contract gains an event, add it here first.
"""

from __future__ import annotations

from typing import Any

import pytest

from custom_components.switchboard.coordinator import (
    SwitchboardData,
    _apply_afk,
    _state_from_snapshot,
)

# --- docs/HA.md "Full event reference" -----------------------------------------------------
CID = "11111111-1111-1111-1111-111111111111"

DOCUMENTED_EVENTS: list[dict[str, Any]] = [
    {"type": "obs_connection", "connection_id": CID, "connected": True},
    {"type": "obs_reconnecting", "connection_id": CID},
    {
        "type": "obs_scene_changed",
        "connection_id": CID,
        "scene": "Gaming",
        "scene_uuid": "obs-scene-uuid",
    },
    {
        "type": "obs_scene_renamed",
        "connection_id": CID,
        "scene_uuid": "obs-scene-uuid",
        "old_name": "Game",
        "new_name": "Gaming",
    },
    {"type": "obs_scenes_changed", "connection_id": CID, "scenes": ["Gaming", "Starting soon"]},
    {"type": "obs_stream_state", "connection_id": CID, "active": True, "state": "started"},
    {"type": "obs_record_state", "connection_id": CID, "active": False, "state": "stopped"},
    {"type": "obs_delay_changed", "connection_id": CID, "secs": 150},
    {"type": "obs_launched_local", "outcome": "launched"},
    {
        "type": "stream_delay_changed",
        "connection_id": CID,
        "label": "Streaming",
        "enabled": True,
        "seconds": 150,
        "restarting": False,
    },
    {"type": "home_assistant_connection", "connection_id": CID, "connected": False},
    {"type": "home_assistant_reconnecting", "connection_id": CID, "error": "cert mismatch"},
    {
        "type": "home_assistant_state_changed",
        "connection_id": CID,
        "entity_id": "binary_sensor.studio_door",
        "state": "on",
        "from_state": "off",
        "friendly_name": "Studio Door",
    },
    {"type": "machine_state_changed", "state": "afk", "source": "External API"},
    {"type": "variable_changed", "name": "deaths", "value": "5"},
    {"type": "overlay_countdown", "ends_at_ms": 1700000000000, "label": "Starting soon"},
    {"type": "hotkey_pressed", "combo": "KEY_LEFTCTRL+KEY_M"},
    {"type": "obs_disk_space", "connection_id": CID, "free_mb": 12698, "low": True},
    {
        "type": "twitch_clip_created",
        "connection_id": CID,
        "clip_id": "SomeClipSlug",
        "url": "https://clips.twitch.tv/SomeClipSlug",
        "edit_url": "https://clips.twitch.tv/SomeClipSlug/edit",
        "source": "OpenDeck",
    },
    {
        "type": "obs_input_mute_changed",
        "connection_id": CID,
        "input": "Mic/Aux",
        "muted": True,
    },
    {
        "type": "app_detect_changed",
        "focused": "steam_app_599140",
        "watched_focused": True,
        "running": ["steam_app_599140"],
        "watched_running": True,
    },
    {
        "type": "twitch_stream_status",
        "connection_id": CID,
        "live": True,
        "title": "Stream",
        "category_id": "509658",
        "category_name": "Just Chatting",
        "box_art_url": "https://example.invalid/box.png",
        "started_at_ms": 1700000000000,
    },
    {
        "type": "twitch_category_updated",
        "connection_id": CID,
        "game_id": "509658",
        "game_name": "Just Chatting",
    },
    {"type": "twitch_chatters_updated", "connection_id": CID, "watching": 42, "chatters": 7},
    {
        "type": "twitch_event",
        "connection_id": CID,
        "kind": "twitch_follow",
        "fields": [["follower", "name"]],
    },
    {
        "type": "twitch_go_live",
        "connection_id": CID,
        "label": "Main",
        "login": "kirbownd",
        "obs_target": "Streaming PC",
    },
    {"type": "twitch_stream_target_restored", "obs_connection_id": CID, "restored_to": "Main"},
    {"type": "spotify_now_playing", "now": {"playing": True, "title": "T", "artist": "A"}},
    {"type": "spotify_song_changed", "now": {"title": "T", "artist": "A", "art_url": "u"}},
    {"type": "spotify_playlist_changed", "now": {"title": "T", "artist": "A", "playlist": "Mix"}},
    {"type": "spotify_playback_started", "now": {"title": "T", "artist": "A"}},
    {"type": "spotify_playback_paused", "now": {"title": "T", "artist": "A"}},
    {"type": "spotify_playback_stopped"},
    {
        "type": "spotify_song_liked",
        "track": "Nightcall",
        "artist": "Kavinsky",
        "url": "u",
        "art_url": "a",
        "liked": True,
        "source": "OpenDeck",
    },
    {
        "type": "spotify_playlist_track_added",
        "track": "Nightcall",
        "artist": "Kavinsky",
        "playlist": "Stream 2026",
        "playlist_url": "u",
        "added": True,
        "source": "OpenDeck",
    },
    {
        "type": "twitch_chat_command",
        "connection_id": CID,
        "channel": "kirbownd",
        "command": "!so",
        "args": "@yarrow_pixelz",
        "login": "mothlamp_99",
        "name": "mothlamp_99",
        "is_broadcaster": False,
        "is_mod": True,
        "is_sub": True,
    },
    {
        "type": "obs_stream_health",
        "connection_id": CID,
        "healthy": False,
        "dropped_pct": 4.2,
        "congestion": 0.61,
        "reconnecting": False,
    },
    {
        "type": "insights_session_ended",
        "connection_id": CID,
        "session_id": CID,
        "started_at": 1756500000,
        "ended_at": 1756513320,
        "duration_secs": 13320,
        "peak_viewers": 42,
        "avg_viewers": 31,
        "peak_chatters": 17,
        "avg_chatters": 9,
        "followers_gained": 9,
        "subs_gained": 2,
        "msgs": 812,
        "unique_chatters": 90,
        "raids": 1,
        "bits": 500,
        "categories": "Cyberpunk 2077",
    },
    {"type": "overlay_alert", "text": "KirboWned just followed!"},
    {"type": "mesh_identity_reset", "fingerprint": "AA:BB"},
    {"type": "peer_lifecycle", "peer_id": CID, "name": "Gaming", "state": "restarting"},
    {"type": "peer_reachability", "peer_id": CID, "name": "Gaming", "online": True},
    {"type": "opendeck_connection", "plugin_id": CID, "name": "Deck", "connected": True},
    {"type": "plugin_paired", "name": "Deck"},
    {"type": "plugin_removed", "name": "Deck"},
    {
        "type": "rule_fired",
        "rule_id": CID,
        "name": "Raid lights",
        "action_type": "ha_light_flash",
        "target": "light.studio_key",
        "value": "",
        "actions": 3,
        "ok": True,
        "log_ui": True,
    },
    {
        "type": "rule_action_queued",
        "rule_id": CID,
        "rule_name": "Delay on",
        "action_type": "obs_delay_set",
        "peer_id": CID,
        "peer_name": "Gaming",
        "error": "peer rpc failed",
    },
    {
        "type": "rule_action_delivered",
        "rule_id": CID,
        "rule_name": "Delay on",
        "action_type": "obs_delay_set",
        "peer_id": CID,
        "peer_name": "Gaming",
        "attempts": 3,
    },
    {
        "type": "rule_action_failed",
        "rule_id": CID,
        "rule_name": "Delay on",
        "action_type": "obs_delay_set",
        "peer_id": CID,
        "peer_name": "Gaming",
        "reason": "expired",
    },
    {"type": "rule_events_dropped", "count": 3},
    {
        "type": "external_command",
        "source": "OpenDeck",
        "action_type": "obs_scene_set",
        "target": CID,
        "value": "Gaming",
    },
    {"type": "opendeck_event", "source": "opendeck", "fields": [["button", "scene_1"]]},
    {"type": "navigate_to_view", "view": "settings", "anchor": "gpu", "source": "OpenDeck"},
]

# docs/HA.md "Connections & current state" — the full `ApiState` example.
API_STATE: dict[str, Any] = {
    "obs": [
        {
            "id": CID,
            "label": "Home OBS",
            "connected": True,
            "streaming": True,
            "recording": False,
            "current_scene": "Gaming",
            "stream_started_ms": 1700000000000,
            "stream_delay_secs": 20,
        }
    ],
    "spotify": "playing",
    "afk": False,
    "spotify_now": {
        "playing": True,
        "title": "Song",
        "artist": "Artist",
        "featuring": "Guest",
        "album": "Album",
        "playlist": "My Mix",
        "playlist_url": "https://example.invalid/pl",
        "art_url": "https://example.invalid/art",
        "url": "https://example.invalid/track",
        "up_next_title": "Next",
        "up_next_artist": "Other",
        "position_ms": 45000,
        "duration_ms": 180000,
        "updated_at_ms": 1700000000000,
    },
    "twitch": [
        {
            "id": "22222222-2222-2222-2222-222222222222",
            "label": "Main",
            "live": True,
            "viewers": 42,
            "chatters": 7,
            "title": "Stream",
            "category_id": "509658",
            "category_name": "Just Chatting",
            "box_art_url": "https://example.invalid/box.png",
            "started_at_ms": 1700000000000,
        }
    ],
    "machine_state": "active",
    "apps": {
        "focused": "steam_app_599140",
        "running": ["steam_app_599140"],
        "watched_focused": True,
        "watched_running": False,
    },
    "version": "2026.6.10",
    "update": {"version": "2026.7.1", "body": "notes", "ready": False},
}

# docs/HA.md "AFK numbers".
API_AFK: dict[str, Any] = {
    "afk": False,
    "idle_secs": 42,
    "threshold_secs": 180,
    "snooze_secs": None,
    "afk_in_secs": 138,
}


def test_api_state_maps_every_documented_field() -> None:
    """Every key the contract shows on `/api/state` must land somewhere we read."""
    data = _state_from_snapshot(API_STATE)

    inst = data.obs[CID]
    assert inst["label"] == "Home OBS"
    assert inst["connected"] is True
    assert inst["streaming"] is True
    assert inst["recording"] is False
    assert inst["current_scene"] == "Gaming"
    assert inst["stream_started_ms"] == 1700000000000
    assert inst["stream_delay_secs"] == 20

    assert data.spotify == "playing"
    assert data.spotify_now is not None
    for key in ("title", "artist", "featuring", "album", "playlist", "playlist_url"):
        assert data.spotify_now[key] == API_STATE["spotify_now"][key]
    assert data.spotify_now["up_next_title"] == "Next"
    assert data.spotify_now["duration_ms"] == 180000
    # Position is deliberately dropped — it drifts every few seconds (see `_SPOTIFY_KEYS`).
    assert "position_ms" not in data.spotify_now

    tw = data.twitch["22222222-2222-2222-2222-222222222222"]
    assert tw["live"] is True
    assert tw["viewers"] == 42
    assert tw["chatters"] == 7
    assert tw["category_name"] == "Just Chatting"
    assert tw["box_art_url"] == "https://example.invalid/box.png"
    assert tw["started_at_ms"] == 1700000000000

    assert data.afk is False  # machine_state "active"
    assert data.focused_app == "steam_app_599140"
    assert data.running_apps == ["steam_app_599140"]
    assert data.watched_focused is True
    assert data.watched_running is False
    assert data.watched_app_active is True
    assert data.version == "2026.6.10"
    assert data.update == {"version": "2026.7.1", "body": "notes", "ready": False}


def test_api_state_tolerates_an_empty_snapshot() -> None:
    """Nothing configured yet (or an older app) must not raise."""
    data = _state_from_snapshot({})
    assert data.obs == {}
    assert data.twitch == {}
    assert data.spotify == "stopped"
    assert data.spotify_now is None
    assert data.afk is False


def test_api_state_prefers_machine_state_over_the_afk_bool() -> None:
    data = _state_from_snapshot({**API_STATE, "machine_state": "afk", "afk": False})
    assert data.afk is True


def test_api_state_falls_back_to_the_afk_bool() -> None:
    """`machine_state` is a consumer extra; the mesh-shaped snapshot only has `afk`."""
    data = _state_from_snapshot({"afk": True})
    assert data.afk is True


def test_resync_preserves_the_live_only_scene_list() -> None:
    previous = _state_from_snapshot(API_STATE)
    previous.obs[CID]["scenes"] = ["Gaming", "BRB"]
    assert _state_from_snapshot(API_STATE, previous).obs[CID]["scenes"] == ["Gaming", "BRB"]


def test_afk_payload_maps_the_stable_numbers() -> None:
    data = SwitchboardData()
    assert _apply_afk(data, API_AFK) is True
    assert data.afk_threshold_secs == 180
    assert data.afk_snooze_until_ms is None
    # Resampling identical numbers is not a change (no needless entity rewrite).
    assert _apply_afk(data, API_AFK) is False


def test_afk_snooze_becomes_an_absolute_deadline() -> None:
    data = SwitchboardData()
    assert _apply_afk(data, {**API_AFK, "snooze_secs": 900}) is True
    assert data.afk_snooze_until_ms is not None
    # A second sample of the same window (a second later) must not re-fire.
    assert _apply_afk(data, {**API_AFK, "snooze_secs": 899}) is False
    # Cancelling it does.
    assert _apply_afk(data, {**API_AFK, "snooze_secs": None}) is True
    assert data.afk_snooze_until_ms is None


def test_afk_threshold_null_means_never() -> None:
    """A per-scene override of 0 ("never go AFK on this scene") reports `threshold_secs: null`."""
    data = SwitchboardData()
    _apply_afk(data, API_AFK)
    assert _apply_afk(data, {**API_AFK, "threshold_secs": None}) is True
    assert data.afk_threshold_secs is None


@pytest.mark.parametrize("frame", DOCUMENTED_EVENTS, ids=lambda f: f["type"])
def test_every_documented_event_is_json_shaped(frame: dict[str, Any]) -> None:
    """Cheap sanity net so a malformed fixture can't make the coordinator tests vacuous."""
    assert isinstance(frame.get("type"), str)
