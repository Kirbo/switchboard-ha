"""Constants for the Switchboard integration."""

from __future__ import annotations

DOMAIN = "switchboard"

# Switchboard's external API + peer mesh share this TLS port.
DEFAULT_PORT = 38474

# Config-entry data keys.
CONF_FINGERPRINT = "fingerprint"  # optional SHA-256 cert pin (hex, ':'-separated ok)
# (host/port/token/verify_ssl reuse homeassistant.const CONF_* names)

# Options (entry.options) keys.
# Whether `twitch_chat_message` reaches the HA bus with its `author`/`text` intact. Off by default:
# Home Assistant's recorder archives every non-excluded custom event for `purge_keep_days`, so
# re-firing chat verbatim builds a per-line chat archive on the HA box — the very thing the app
# refuses to write to its own log under any setting (SB-D-022). The frame itself still arrives
# (redacted) so chat can be counted.
CONF_MIRROR_CHAT_TEXT = "mirror_chat_text"

# Whether `twitch_moderation` reaches the HA bus with the viewer it names (`target_login`,
# `target_user_id`, `reason`, `error`). Off by default for the same recorder reason: re-firing it
# verbatim keeps a per-viewer moderation record on the HA box that the app keeps out of its own
# log file. The frame still arrives (action/outcome intact) so automations can react to it.
CONF_MIRROR_MODERATION_TARGETS = "mirror_moderation_targets"

# Whether Home Assistant users who are NOT administrators may call the switchboard.* services.
# Off by default (SB-00231): every service runs with the Switchboard API token this entry holds —
# usually the GLOBAL one, which reaches every action, including `ha_service_call` back into Home
# Assistant with the (often admin) HA token stored in Switchboard. A household member's account or
# a wall tablet must not inherit that by default. Calls with no user (automations, the
# system — a script started by a user carries that user) stay allowed either way, like Home
# Assistant's own admin services.
CONF_ALLOW_NON_ADMIN_SERVICES = "allow_non_admin_services"

# Repairs issue keys (strings.json `issues`), suffixed with the entry id per instance.
ISSUE_FINGERPRINT_MISMATCH = "fingerprint_mismatch"

# Event fired on the HA bus for every frame from /api/events/ws, so users can write
# automations on raw Switchboard events (twitch_event, rule_fired, …).
EVENT_SWITCHBOARD = "switchboard_event"

# Spotify playback gate values mirrored from the backend (engine::PlaybackGate, snake_case).
SPOTIFY_PLAYING = "playing"
SPOTIFY_PAUSED = "paused"
SPOTIFY_STOPPED = "stopped"
