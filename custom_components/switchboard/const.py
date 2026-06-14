"""Constants for the Switchboard integration."""

from __future__ import annotations

DOMAIN = "switchboard"

# Switchboard's external API + peer mesh share this TLS port.
DEFAULT_PORT = 38474

# Config-entry data keys.
CONF_FINGERPRINT = "fingerprint"  # optional SHA-256 cert pin (hex, ':'-separated ok)
# (host/port/token/verify_ssl reuse homeassistant.const CONF_* names)

# Event fired on the HA bus for every frame from /api/events/ws, so users can write
# automations on raw Switchboard events (twitch_event, rule_fired, …).
EVENT_SWITCHBOARD = "switchboard_event"

# Spotify playback gate values mirrored from the backend (engine::PlaybackGate, snake_case).
SPOTIFY_PLAYING = "playing"
SPOTIFY_PAUSED = "paused"
SPOTIFY_STOPPED = "stopped"
