"""State coordinator: initial REST snapshot + a long-lived events websocket.

Push integration. `_async_update_data` runs once (initial `/api/state` + `/api/connections`);
after that a background task streams `/api/events/ws`, patches the snapshot in place, and calls
`async_set_updated_data` so entities update. Every frame is also re-fired on the HA bus as
`switchboard_event` for user automations.

Against an app whose frames carry `seq` / `boot_id`, a reconnect RESUMES instead (docs/HA.md,
"Resume"): it asks for `?resume=<last seq>&boot=<boot_id>`, the app replays what was missed and
says `resumed`, or says `gap` and the snapshot is re-read. Replayed frames apply once each (dedupe
by seq); one older than the `late_event_max_age` option updates the entities but is not fired on
the bus. The position lives in memory only, so a reload or an HA restart re-seeds.

Otherwise the snapshot is re-read on every connect and whenever the app sends a `resync` frame (its
socket fell behind and dropped events — docs/HA.md, "resync"). Both re-reads are awaited INSIDE the
frame loop, so frames that arrive while the GETs are in flight wait in the socket's receive
queue and are applied over the fresh snapshot afterwards, in arrival order: that queue is the
buffer that keeps an older snapshot from overwriting a newer event. Never move a re-read into a
detached task — the frames after it would then be applied first and lost under the snapshot.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
import logging
import random
import time
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EVENT_CALL_SERVICE,
    EVENT_HOMEASSISTANT_STOP,
    RESTART_EXIT_CODE,
)
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    SwitchboardAccessError,
    SwitchboardApiError,
    SwitchboardAuthError,
    SwitchboardClient,
)
from .const import (
    CONF_LATE_EVENT_MAX_AGE,
    CONF_MIRROR_CHAT_TEXT,
    CONF_MIRROR_MODERATION_TARGETS,
    DEFAULT_LATE_EVENT_MAX_AGE,
    DOMAIN,
    EVENT_SWITCHBOARD,
    ISSUE_FINGERPRINT_MISMATCH,
    SPOTIFY_PAUSED,
    SPOTIFY_PLAYING,
    SPOTIFY_STOPPED,
)

_LOGGER = logging.getLogger(__name__)

# Reconnect backoff ladder (seconds): never stops retrying, capped at 60s. Reset to step 0 once a
# connect is fully synced (re-seeded or resumed) — not on the bare socket open, or an app that
# accepts the socket but fails every seed read would be redialled once a second forever. Matches
# the Switchboard app's own app→HA ladder. Each wait is spread ±20 % (`RECONNECT_JITTER`) so a
# restarting app is not redialled by every client in the same instant.
RECONNECT_LADDER = (1, 2, 3, 5, 10, 15, 30, 45, 60)
RECONNECT_JITTER = 0.2

# How long an app asked to resume (docs/HA.md "Resume") may take to answer `resumed` / `gap`. An
# app that says neither has not honoured the resume: the socket is dropped and the next connect
# re-seeds from /api/state instead.
RESUME_TIMEOUT_SECS = 15.0

# The goodbye frame (docs/HA.md, `goodbye`) is fire-and-forget: HA is shutting down and must never
# wait on the app. One second caps the send even on a stalled socket.
GOODBYE_TIMEOUT_SECS = 1.0

# A `homeassistant.restart` service call marks the NEXT stop as a restart for this long. Needed
# because on a Supervisor install (HA OS / Supervised) that service asks the Supervisor to restart
# the container, which stops Core with SIGTERM — exit code 0, indistinguishable from a plain stop.
# Bounded so a restart that was refused (HA's config check failed) does not label a stop days later.
RESTART_REQUEST_TTL_SECS = 120.0

# How long an /api/afk resample waits before firing, so a burst of scene changes costs one
# request instead of one per event.
AFK_COALESCE_SECS = 1.0

# Events that patch one OBS instance (all keyed by `connection_id`).
_OBS_EVENTS = (
    "obs_scene_changed",
    "obs_connection",
    "obs_stream_state",
    "obs_record_state",
    "obs_scenes_changed",
    "obs_delay_changed",
    "stream_delay_changed",
    "obs_frozen",
)

# Events carrying a full NowPlaying payload for a track/context change.
_SPOTIFY_TRACK_EVENTS = (
    "spotify_song_changed",
    "spotify_playlist_changed",
    "spotify_playback_started",
    "spotify_now_playing",
)


@dataclass
class SwitchboardData:
    """Normalised, entity-friendly view of a Switchboard machine snapshot."""

    obs: dict[str, dict[str, Any]] = field(default_factory=dict)  # connection_id -> fields
    spotify: str = SPOTIFY_STOPPED
    spotify_now: dict[str, Any] | None = None
    afk: bool = False
    twitch: dict[str, dict[str, Any]] = field(default_factory=dict)  # connection_id -> live data
    version: str = ""
    update: dict[str, Any] | None = None  # {version, body, ready} or None
    # App-detection: the focused app id (or None), the running watch-list ids, and the two
    # separate "a watched app is …" flags the contract distinguishes.
    focused_app: str | None = None
    running_apps: list[str] = field(default_factory=list)
    watched_focused: bool = False
    watched_running: bool = False
    # AFK numbers sampled from GET /api/afk. Only the two that are STABLE between samples are
    # kept: `threshold_secs` (soonest idle→AFK rule threshold, scene override applied; None =
    # nothing will auto-set AFK) and the snooze window turned into an ABSOLUTE deadline so a
    # stale sample can't lie about how much is left. `idle_secs`/`afk_in_secs` change every
    # second by design and are deliberately not mirrored — poll /api/afk for a live countdown.
    afk_threshold_secs: int | None = None
    afk_snooze_until_ms: int | None = None
    # Switchboard's user variables (name -> value, values are strings), seeded from /api/state's
    # `variables` and kept live by `variable_changed` — the README promises `data["variables"]`
    # to template sensors, and until 2026-09-02 nothing populated it.
    variables: dict[str, str] = field(default_factory=dict)
    # The keyboard/mouse/gamepad input overlays are shown (2026-09-19, additive on /api/state;
    # absent on older apps = shown). `input_overlay_auto_show_at_ms` = when the app's own
    # auto-show timer brings them back (None = no timer). Kept live by `input_overlay_visibility`.
    input_overlay_visible: bool = True
    input_overlay_auto_show_at_ms: int | None = None
    # Alert presets' queues (app 2026-10-15, additive on /api/state as `alerts`; absent on older
    # apps = no presets): preset id -> {name, queued, paused, showing, dropped, merged}. Seeded
    # from the snapshot, kept live by `alert_queue_changed`; a deleted preset's last all-zero
    # frame drops its row (and the entities built on it go with it).
    alerts: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Every connection, subscription and background loop of the app with its state and session
    # counters (app 2026-10-18, additive on /api/state as `connection_status`, an array): key ->
    # row. `None` = an app older than the contract (the sensor then reads unknown). Seeded from
    # the snapshot, kept live by `connection_status_changed` (a whole row, replaced by key) and
    # `connection_status_removed` (drop by key).
    connection_status: dict[str, dict[str, Any]] | None = None

    @property
    def watched_app_active(self) -> bool:
        """A watched app is in play (foreground OR running)."""
        return self.watched_focused or self.watched_running


_TWITCH_KEYS = (
    "label",
    "live",
    "viewers",
    "chatters",
    "title",
    "category_id",
    "category_name",
    "box_art_url",
    "started_at_ms",
    # Audience totals (null until the tracker's first cycle); live via `twitch_audience_totals`.
    "followers",
    "subs",
)

# The now-playing fields we surface. `position_ms`/`updated_at_ms` are deliberately dropped:
# `spotify_now_playing` is re-emitted on position drift, so mirroring them would rewrite the
# sensor (and its recorder history) every few seconds for no visible change.
_SPOTIFY_KEYS = (
    "playing",
    "title",
    "artist",
    "featuring",
    "album",
    "playlist",
    "playlist_url",
    "art_url",
    "url",
    "up_next_title",
    "up_next_artist",
    "duration_ms",
)


def _alert_view(raw: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """One `/api/state.alerts` row (or an `alert_queue_changed` frame) → our per-preset fields.

    Every field is coerced to the contract's type so a missing key on a frame reads as the idle
    value instead of raising. A frame without `name` keeps the name we already had.
    """

    def count(key: str) -> int:
        value = raw.get(key)
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0

    name = raw.get("name")
    if not isinstance(name, str):
        name = (previous or {}).get("name") or ""
    showing = raw.get("showing")
    return {
        "name": name,
        "queued": count("queued"),
        "paused": raw.get("paused") is True,
        "showing": showing if isinstance(showing, str) and showing else None,
        "dropped": count("dropped"),
        "merged": count("merged"),
    }


def _is_deleted_alert_frame(frame: dict[str, Any]) -> bool:
    """docs/HA.md "Alert presets": a deleted preset sends one last frame with everything
    zero/`false`/`null` and an EMPTY `name`, then drops out of `/api/state`."""
    return (
        frame.get("name") == ""
        and not frame.get("queued")
        and not frame.get("paused")
        and frame.get("showing") is None
        and not frame.get("dropped")
        and not frame.get("merged")
    )


# docs/HA.md "Connection status": the states, and how loudly each reads (higher is worse; a
# switched-off row is quieter than a connected one).
STATUS_SEVERITY: dict[str, int] = {
    "disabled": 0,
    "connected": 1,
    "connecting": 2,
    "reconnecting": 3,
    "down": 4,
    "crashed": 5,
}


def _status_row(raw: Any) -> dict[str, Any] | None:
    """One `/api/state.connection_status` row (or a `connection_status_changed` entry) → our
    fields; `None` without a key. An unknown state reads as `down`: a consumer must never show a
    state it cannot name as fine."""
    if not isinstance(raw, dict) or not isinstance(raw.get("key"), str) or not raw["key"]:
        return None

    def count(key: str) -> int:
        value = raw.get(key)
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0

    def text(key: str) -> str | None:
        value = raw.get(key)
        return value if isinstance(value, str) and value else None

    state = raw.get("state")
    return {
        "key": raw["key"],
        "integration": text("integration") or "system",
        "component": text("component") or "",
        "connection_id": text("connection_id"),
        "subject": text("subject"),
        "label": text("label"),
        "state": state if state in STATUS_SEVERITY else "down",
        "last_error": text("last_error"),
        "drops": count("drops"),
        "reconnects": count("reconnects"),
        "crashes": count("crashes"),
        # app 2026-10-19+, additive: `false` = the user took the row out of "needs attention" (a
        # peer that is normally off); absent (an older app) = counted.
        "counted": raw.get("counted") is not False,
    }


def _status_from_snapshot(raw: Any) -> dict[str, dict[str, Any]] | None:
    """`/api/state.connection_status` (an array) → key -> row; `None` when the app does not send
    it (older than 2026-10-18)."""
    if not isinstance(raw, list):
        return None
    rows = (_status_row(r) for r in raw)
    return {r["key"]: r for r in rows if r is not None}


def worst_status(rows: dict[str, dict[str, Any]]) -> str | None:
    """The worst state among the COUNTED rows (docs/HA.md ranking); `disabled` when rows exist but
    none counts (an uncounted row never makes anything look worse — the app's rule); `None` for
    no rows."""
    if not rows:
        return None
    states = [r["state"] for r in rows.values() if r.get("counted", True)]
    return max(states, key=lambda s: STATUS_SEVERITY.get(s, 4)) if states else "disabled"


def _alerts_from_snapshot(raw: Any) -> dict[str, dict[str, Any]]:
    """`/api/state.alerts` → preset id -> row. Absent (an app older than 2026-10-15) or malformed
    reads as "no presets", never an error."""
    if not isinstance(raw, dict):
        return {}
    return {str(pid): _alert_view(row) for pid, row in raw.items() if isinstance(row, dict)}


@callback
def _is_restart_call(event_data: Any) -> bool:
    return event_data.get("domain") == "homeassistant" and event_data.get("service") == "restart"


def _is_resync(frame: Any) -> bool:
    """The app's `{"type": "resync"}` frame (docs/HA.md, "resync", app 2026-10-13): not an event
    — it concerns this one socket — so it is never re-fired on the HA bus or patched in."""
    return isinstance(frame, dict) and frame.get("type") == "resync"


def _frame_seq(frame: dict[str, Any]) -> tuple[int, str] | None:
    """(seq, boot_id) of an event frame from a resume-capable app (docs/HA.md "Resume"), or None
    for an older app's frame or a control frame."""
    seq = frame.get("seq")
    boot = frame.get("boot_id")
    if isinstance(seq, bool) or not isinstance(seq, int) or not isinstance(boot, str) or not boot:
        return None
    return seq, boot


def _frame_epoch_secs(ts: Any) -> float | None:
    """A frame's `ts` as epoch seconds: epoch milliseconds (any number past 1e11 — year 5138 in
    seconds), epoch seconds, or an ISO-8601 string. None when it is none of those."""
    if isinstance(ts, bool):
        return None
    if isinstance(ts, (int, float)):
        return ts / 1000 if ts > 1e11 else float(ts)
    if isinstance(ts, str):
        parsed: datetime | None = dt_util.parse_datetime(ts)
        if parsed is not None:
            return parsed.timestamp()
    return None


def _spotify_view(now: dict[str, Any] | None) -> dict[str, Any] | None:
    """Trim a NowPlaying payload to the fields we expose (see `_SPOTIFY_KEYS`)."""
    if not now:
        return None
    return {k: now.get(k) for k in _SPOTIFY_KEYS}


def _spotify_gate(now: dict[str, Any] | None) -> str:
    """`now is None` means nothing is playing (the app's SpotifyNowPlaying contract) → the gate
    is "stopped", not "paused". A present-but-not-playing track is paused."""
    if not now:
        return SPOTIFY_STOPPED
    return SPOTIFY_PLAYING if now.get("playing") else SPOTIFY_PAUSED


def _obs_from_snapshot(o: dict[str, Any]) -> dict[str, Any]:
    """One `ApiState.obs[]` entry → our per-instance fields. `scenes` has no snapshot field
    (only the `obs_scenes_changed` event carries it), so it starts empty and fills in live."""
    return {
        "label": o.get("label", o["id"]),
        "connected": o.get("connected", False),
        "streaming": o.get("streaming", False),
        "recording": o.get("recording", False),
        "current_scene": o.get("current_scene"),
        "stream_started_ms": o.get("stream_started_ms"),
        "stream_delay_secs": o.get("stream_delay_secs"),
        # OBS itself is frozen (app 2026-10-17, additive; absent from older apps → False).
        "frozen": o.get("frozen", False),
        "scenes": [],
    }


def _apply_afk(data: SwitchboardData, raw: dict[str, Any]) -> bool:
    """Fold a `GET /api/afk` payload into `data`. Returns True when something we expose moved.

    `snooze_secs` is a countdown, so it is stored as an absolute deadline: a sample taken two
    minutes ago then still reads correctly instead of claiming a window that already elapsed.
    """
    threshold = raw.get("threshold_secs")
    threshold = None if threshold is None else int(threshold)
    snooze = raw.get("snooze_secs")
    until = None if snooze is None else int(time.time() * 1000) + int(snooze) * 1000

    changed = threshold != data.afk_threshold_secs
    data.afk_threshold_secs = threshold

    was = data.afk_snooze_until_ms
    if (until is None) != (was is None):
        data.afk_snooze_until_ms = until
        changed = True
    elif until is not None and was is not None and abs(until - was) > 2000:
        # Deadlines drift by the request round-trip; only a >2 s move is a real re-snooze, so
        # resampling an unchanged window doesn't rewrite the entity.
        data.afk_snooze_until_ms = until
        changed = True
    return changed


def _state_from_snapshot(
    raw: dict[str, Any], previous: SwitchboardData | None = None
) -> SwitchboardData:
    obs: dict[str, dict[str, Any]] = {}
    for o in raw.get("obs", []):
        inst = _obs_from_snapshot(o)
        # Carry the live-only scene list across a resync so it doesn't blank on every reconnect.
        if previous and (old := previous.obs.get(o["id"])):
            inst["scenes"] = old.get("scenes") or []
        obs[o["id"]] = inst
    twitch: dict[str, dict[str, Any]] = {}
    for tw in raw.get("twitch", []):
        twitch[tw["id"]] = {k: tw.get(k) for k in _TWITCH_KEYS}
    apps = raw.get("apps") or {}
    return SwitchboardData(
        obs=obs,
        spotify=raw.get("spotify", SPOTIFY_STOPPED),
        spotify_now=_spotify_view(raw.get("spotify_now")),
        # `afk` and `machine_state` mirror each other server-side; prefer the explicit string
        # when present so the two can never be read inconsistently.
        afk=(raw["machine_state"] == "afk") if "machine_state" in raw else raw.get("afk", False),
        twitch=twitch,
        version=raw.get("version", ""),
        update=raw.get("update"),
        focused_app=apps.get("focused"),
        running_apps=list(apps.get("running") or []),
        watched_focused=bool(apps.get("watched_focused")),
        watched_running=bool(apps.get("watched_running")),
        variables={str(k): str(v) for k, v in (raw.get("variables") or {}).items()},
        input_overlay_visible=raw.get("input_overlay_visible", True) is not False,
        input_overlay_auto_show_at_ms=raw.get("input_overlay_auto_show_at_ms"),
        alerts=_alerts_from_snapshot(raw.get("alerts")),
        connection_status=_status_from_snapshot(raw.get("connection_status")),
        # Preserved across a resync — /api/state doesn't carry them, /api/afk does.
        afk_threshold_secs=previous.afk_threshold_secs if previous else None,
        afk_snooze_until_ms=previous.afk_snooze_until_ms if previous else None,
    )


class SwitchboardCoordinator(DataUpdateCoordinator[SwitchboardData]):
    """Owns the client, the snapshot, and the events websocket task."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: SwitchboardClient,
    ) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=None)
        self.entry = entry
        self.client = client
        self.connections: list[dict[str, Any]] = []
        self._closing = False
        # Set by HA's stop: the loop's leftover errors are expected and logged at DEBUG only.
        self._ha_stopping = False
        self._ws_task: asyncio.Task[None] | None = None
        # The events socket while it is open — the goodbye frame goes out on it.
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        # HA's stop listener + the restart-request watcher (removed on unload, so a reloaded
        # entry never says goodbye twice).
        self._stop_unsubs: list[CALLBACK_TYPE] = []
        self._restart_requested_at: float | None = None
        self._refreshing = False
        self._refresh_again = False
        self._afk_task: asyncio.Task[None] | None = None
        self._afk_again = False
        # Once-per-outage log gates — reset after a fully successful connect (ws + resync).
        self._access_denied_logged = False
        self._reauth_logged = False
        self._fingerprint_mismatch_logged = False
        # The reconnect backoff's wait — `asyncio.sleep`; a test injects a recorder so the ladder
        # can be asserted without waiting it out.
        self._backoff_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
        # The [0, 1) draw that jitters each wait — `random.random`; a test pins it.
        self._jitter: Callable[[], float] = random.random
        # The wall clock a replayed frame's age is measured against — a test pins it.
        self._now: Callable[[], float] = time.time
        # The resume position (docs/HA.md "Resume"): the newest event `seq` seen and the app run
        # (`boot_id`) it belongs to. Memory only — an entry reload or an HA restart re-seeds, which
        # is always correct. None against an app whose frames carry no `seq` (it predates resume):
        # every reconnect then re-seeds, exactly as before.
        self._last_seq: int | None = None
        self._boot_id: str | None = None
        # This socket asked to resume and the app has answered neither `resumed` nor `gap` yet.
        self._awaiting_resume = False

    async def _async_update_data(self) -> SwitchboardData:
        try:
            self.connections = await self.client.fetch_connections()
            raw = await self.client.fetch_state()
        except SwitchboardApiError as err:
            raise UpdateFailed(str(err)) from err
        data = _state_from_snapshot(raw, self.data)
        try:
            _apply_afk(data, await self.client.fetch_afk())
        except SwitchboardApiError as err:
            # Never fail setup on the AFK extras — they are attributes on an entity whose main
            # state came from /api/state, which already succeeded.
            _LOGGER.debug("switchboard: /api/afk unavailable: %s", err)
        return data

    async def async_start(self) -> None:
        """Launch the events websocket as a background task."""
        self._ws_task = self.hass.async_create_background_task(
            self._ws_loop(), f"{DOMAIN}_events_ws"
        )

    async def async_stop(self) -> None:
        """Stop the websocket task (called on entry unload)."""
        self._closing = True
        if self._ws_task:
            self._ws_task.cancel()
            self._ws_task = None
        if self._afk_task:
            self._afk_task.cancel()
            self._afk_task = None

    # --- Home Assistant shutdown: the goodbye frame (docs/HA.md, `goodbye`) -------------------

    @callback
    def async_listen_ha_stop(self) -> None:
        """Say goodbye to the app when Home Assistant stops, so it can treat the drop of its own
        HA connection (and this socket) as an expected restart instead of an outage."""
        self.async_unlisten_ha_stop()
        self._stop_unsubs = [
            self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._async_ha_stopping),
            self.hass.bus.async_listen(
                EVENT_CALL_SERVICE,
                self._async_restart_requested,
                event_filter=_is_restart_call,
            ),
        ]

    @callback
    def async_unlisten_ha_stop(self) -> None:
        for unsub in self._stop_unsubs:
            unsub()
        self._stop_unsubs = []

    @callback
    def _async_restart_requested(self, _event: Event) -> None:
        self._restart_requested_at = time.monotonic()

    def _stop_reason(self) -> str:
        if self.hass.exit_code == RESTART_EXIT_CODE:
            return "restart"
        at = self._restart_requested_at
        if at is not None and time.monotonic() - at <= RESTART_REQUEST_TTL_SECS:
            return "restart"
        return "stop"

    @callback
    def _async_ha_stopping(self, _event: Event) -> None:
        """EVENT_HOMEASSISTANT_STOP. A `@callback` on purpose: HA cancels background tasks (the
        events loop among them) just BEFORE firing this event, and the loop closes the socket as
        soon as it next runs. Running synchronously inside the event fire, with an eagerly started
        send, puts the frame on the transport before that close — a coroutine listener would be
        scheduled after it and find the socket gone."""
        # The fired once-listener is spent; removing it again on a later unload would log an error.
        if self._stop_unsubs:
            self._stop_unsubs.pop(0)
        # HA is going down: the loop must neither reconnect nor warn about the drop (it
        # reconnects on the next start anyway).
        self._closing = True
        self._ha_stopping = True
        ws = self._ws
        if ws is None or ws.closed:
            _LOGGER.debug("switchboard: HA stopping — no open events socket, no goodbye sent")
            return
        reason = self._stop_reason()
        self.hass.async_create_task(
            self._async_send_goodbye(ws, reason), f"{DOMAIN}_goodbye", eager_start=True
        )

    async def _async_send_goodbye(self, ws: aiohttp.ClientWebSocketResponse, reason: str) -> None:
        try:
            async with asyncio.timeout(GOODBYE_TIMEOUT_SECS):
                await ws.send_json({"type": "goodbye", "reason": reason})
        except (TimeoutError, aiohttp.ClientError, ConnectionError, RuntimeError) as err:
            _LOGGER.debug("switchboard: goodbye not sent: %s", err)
        else:
            _LOGGER.debug("switchboard: sent goodbye (%s)", reason)

    # --- connection lookup (used by services) ------------------------------------------------

    def _ids(self, integration: str) -> set[str]:
        return {c["id"] for c in self.connections if c.get("integration") == integration}

    def obs_ids(self) -> set[str]:
        return self._ids("obs")

    def twitch_ids(self) -> set[str]:
        return self._ids("twitch")

    def ha_ids(self) -> set[str]:
        """Downstream Home Assistant connections *of the Switchboard app* — the target of the
        `ha_service_call` / `ha_light_flash` actions (not this integration's own HA)."""
        return self._ids("home_assistant")

    def connection_label(self, connection_id: str) -> str:
        for c in self.connections:
            if c["id"] == connection_id:
                return c.get("label", connection_id)
        return connection_id

    def reauth_needed(self) -> list[dict[str, str]]:
        """Connections the provider REJECTED the credential of (`needs_reauth`, docs/HA.md).

        Not "currently disconnected" (that is the per-integration state in `/api/state`): a
        `needs_reauth` connection cannot come back without a human replacing the credential in
        Switchboard — a Twitch refresh token the authorization no longer covers, or a Home
        Assistant long-lived token the instance answered `auth_invalid` to. The app writes it the
        moment the provider says no, and announces the change as `connections_changed`.
        """
        return [
            {
                "id": c["id"],
                "label": c.get("label", c["id"]),
                "integration": c.get("integration", ""),
            }
            for c in self.connections
            if c.get("needs_reauth")
        ]

    def default_id(self, integration: str) -> str | None:
        """The default connection for an integration — or the only one, which the app treats as
        the implicit default. None when there are several and none is flagged."""
        candidates = [c for c in self.connections if c.get("integration") == integration]
        if len(candidates) == 1:
            return candidates[0]["id"]
        for c in candidates:
            if c.get("is_default"):
                return c["id"]
        return None

    def resolve_connection_id(self, target: str, integration: str | None = None) -> str | None:
        """Accept either a raw id or a friendly label; return the id.

        Returns None when nothing matches (caller decides how to fall back) and raises
        ValueError when a label matches MORE than one connection — the two cases must stay
        distinguishable, or an ambiguous label silently degrades into a bogus id.
        """
        for c in self.connections:
            if c["id"] == target:
                return c["id"]
        matches = [
            c
            for c in self.connections
            if c.get("label") == target
            and (integration is None or c.get("integration") == integration)
        ]
        if len(matches) == 1:
            return matches[0]["id"]
        if not matches:
            return None
        raise ValueError(f"'{target}' is ambiguous — use the connection id")

    # --- events websocket --------------------------------------------------------------------

    def _resume_position(self) -> tuple[int, str] | None:
        if self._last_seq is None or not self._boot_id:
            return None
        return self._last_seq, self._boot_id

    def _accept_seq(self, frame: dict[str, Any]) -> bool:
        """Track the resume position; False = a frame this client already applied (a replay that
        overlaps what it saw before the drop) — dropped, so no sensor moves twice and no
        automation fires twice. Frames without `seq` (an older app) always pass."""
        stamp = _frame_seq(frame)
        if stamp is None:
            return True
        seq, boot = stamp
        if boot == self._boot_id and self._last_seq is not None and seq <= self._last_seq:
            return False
        # A new boot id is a restarted app: its sequence starts over, so adopt it as it comes.
        self._last_seq, self._boot_id = seq, boot
        return True

    def _synced(self) -> None:
        """The mirror is current again — re-seeded, or resumed. Re-arms the once-per-outage log
        gates and retires the fingerprint issue (whatever answered with the wrong certificate is
        gone)."""
        self._awaiting_resume = False
        self._access_denied_logged = False
        self._reauth_logged = False
        self._clear_fingerprint_mismatch()

    async def _ws_loop(self) -> None:
        step = 0
        while not self._closing:
            # A 401 (on the upgrade or a resync read) means the token is gone (docs/HA.md): the
            # reauth flow is the way back — it reloads the entry with the new token, which starts
            # a fresh loop. Retrying a dead token only spends the app's per-token budget and logs
            # an "auth throttled" line per attempt there, so the loop stops (as HA's own
            # coordinator stops refreshing on ConfigEntryAuthFailed).
            token_rejected = False
            resume = self._resume_position()
            try:
                async with (
                    self.client.ws_connect(resume=resume) as ws,
                    # Armed only while a resume is unanswered (see RESUME_TIMEOUT_SECS).
                    asyncio.timeout(None) as resume_deadline,
                ):
                    _LOGGER.debug("switchboard: events websocket connected (resume=%s)", resume)
                    self._ws = ws
                    if resume is None:
                        # No position to resume from (first connect, an older app): re-fetch the
                        # snapshot. The event stream only carries *changes*, so anything that
                        # happened between setup's snapshot (or a disconnect) and this socket
                        # opening was missed and our state is stale.
                        await self._resync()
                        self._synced()
                        step = 0
                    else:
                        # The app replays what we missed, then says `resumed` (entities come back
                        # then) or `gap` (re-seed). Entities stay unavailable until one arrives.
                        self._awaiting_resume = True
                        resume_deadline.reschedule(
                            asyncio.get_running_loop().time() + RESUME_TIMEOUT_SECS
                        )
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                frame = msg.json()
                            except ValueError:  # one bad frame must not kill the loop
                                _LOGGER.exception("switchboard: failed to parse event frame")
                                continue
                            if not isinstance(frame, dict):
                                # The contract only sends event objects; a bare `null`/number/list
                                # is not one (and has no `type` to patch or re-fire by).
                                _LOGGER.debug("switchboard: ignoring a non-object frame: %r", frame)
                                continue
                            if _is_resync(frame):
                                # The app dropped frames this socket should have had; change-only
                                # state would stay stale until it moved again. Awaited inline (see
                                # the module docstring); a failure reconnects, like the one above.
                                _LOGGER.debug("switchboard: app asked for a resync")
                                await self._resync()
                                continue
                            ftype = frame.get("type")
                            if ftype == "resumed":
                                # Every missed frame was replayed: current without a re-read.
                                if self._awaiting_resume:
                                    _LOGGER.debug(
                                        "switchboard: resumed (%s replayed)", frame.get("replayed")
                                    )
                                    resume_deadline.reschedule(None)
                                    self._synced()
                                    step = 0
                                    self.async_set_updated_data(self.data)
                                continue
                            if ftype == "gap":
                                # The app could not resume (too far back, or it restarted).
                                _LOGGER.debug("switchboard: the app could not resume — re-seeding")
                                resume_deadline.reschedule(None)
                                await self._resync()
                                self._synced()
                                step = 0
                                continue
                            if not self._accept_seq(frame):
                                continue  # already applied before the drop
                            try:
                                self._handle_frame(frame)
                            except Exception:  # one bad frame must not kill the loop
                                _LOGGER.exception("switchboard: failed to handle event frame")
                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.CLOSING,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            break
            except asyncio.CancelledError:
                raise
            except aiohttp.WSServerHandshakeError as err:
                # A revoked/rotated token surfaces here as a 401 on the upgrade request.
                if err.status == 401:
                    self._start_reauth()
                    token_rejected = True
                elif err.status == 403:
                    self._log_access_denied()
                else:
                    _LOGGER.debug("switchboard: events websocket handshake failed: %s", err)
            except SwitchboardAuthError:
                # The resync REST calls got a 401 — same revoked-token case.
                self._start_reauth()
                token_rejected = True
            except SwitchboardAccessError:
                # The resync REST calls got a 403 — the token is fine but an ACL denies us.
                self._log_access_denied()
            except aiohttp.ServerFingerprintMismatch as err:
                # BEFORE the generic ClientError arm below, which used to swallow this at DEBUG
                # (SB-E-033): the pinned certificate no longer matches what the host presents.
                self._log_fingerprint_mismatch(err)
            except TimeoutError as err:
                if self._awaiting_resume:
                    # The app took the resume and answered neither `resumed` nor `gap`: forget the
                    # position, so the next connect re-seeds instead of asking again.
                    _LOGGER.debug("switchboard: resume not answered — the next connect re-seeds")
                    self._last_seq = self._boot_id = None
                else:
                    _LOGGER.debug("switchboard: events websocket timed out: %s", err)
            except (aiohttp.ClientError, SwitchboardApiError) as err:
                _LOGGER.debug("switchboard: events websocket dropped: %s", err)
            except Exception:  # this task must never die; entities freeze if it does
                if self._ha_stopping:  # HA is stopping: whatever broke, it is going down anyway
                    _LOGGER.debug("switchboard: events loop error while closing", exc_info=True)
                else:
                    _LOGGER.exception("switchboard: unexpected error in events loop")
            finally:
                self._ws = None
                self._awaiting_resume = False
            if self._closing:
                break
            # Disconnected → entities go unavailable instead of freezing at stale values
            # ("streaming: on" hours after the machine shut down). Restored by the resync above.
            if self.last_update_success:
                self.last_update_success = False
                self.async_update_listeners()
            if token_rejected:
                _LOGGER.debug("switchboard: token rejected — events loop stopped until reauth")
                return
            spread = 1 - RECONNECT_JITTER + 2 * RECONNECT_JITTER * self._jitter()
            await self._backoff_sleep(RECONNECT_LADDER[step] * spread)
            step = min(step + 1, len(RECONNECT_LADDER) - 1)

    def _start_reauth(self) -> None:
        """Kick off the reauth flow (deduped by HA) — the token no longer works.

        The events loop stops right after this (a dead token is not retried; finishing the
        reauth reloads the entry). The log line stays gated once per outage in case another
        path reports the same rejection.
        """
        if self._ha_stopping:  # HA is stopping — a reauth flow now would outlive nothing
            _LOGGER.debug("switchboard: token rejected while closing — no reauth")
            return
        if not self._reauth_logged:
            self._reauth_logged = True
            _LOGGER.warning("switchboard: API token rejected — starting reauthentication")
        self.entry.async_start_reauth(self.hass)

    def _log_access_denied(self) -> None:
        """403: the token is valid but this caller is denied by an ACL. Warn once per outage —
        the retry loop would otherwise repeat it every backoff step."""
        if self._access_denied_logged or self._ha_stopping:
            return
        self._access_denied_logged = True
        _LOGGER.warning(
            "switchboard: access denied (403) — check the Events ACL / allowlist under "
            "Settings → External API; entities stay unavailable until access is restored"
        )

    def _fingerprint_issue_id(self) -> str:
        return f"{ISSUE_FINGERPRINT_MISMATCH}_{self.entry.entry_id}"

    def _log_fingerprint_mismatch(self, err: aiohttp.ServerFingerprintMismatch) -> None:
        """The host answered with a certificate whose SHA-256 is not the pinned one.

        Either Switchboard rotated its identity (a `mesh_identity_reset`, a reinstall) or something
        on the LAN is answering in its place — the integration cannot tell which, so it NEVER
        adopts the new digest on its own: the entry stays unavailable and retries with backoff
        until a human pastes the new fingerprint via Reconfigure. Warn once per outage (the retry
        loop would repeat it every backoff step) and raise a repairs issue so it is visible in the
        UI rather than only in a DEBUG log nobody has enabled.
        """
        seen = err.got.hex()
        if self._fingerprint_mismatch_logged or self._ha_stopping:
            return
        self._fingerprint_mismatch_logged = True
        _LOGGER.warning(
            "switchboard: TLS fingerprint mismatch for %s:%s — the host presented a certificate "
            "with SHA-256 %s, which is not the pinned one; entities stay unavailable until the "
            "entry is reconfigured with a fingerprint confirmed on Switchboard's Peers tab",
            err.host,
            err.port,
            seen,
        )
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            self._fingerprint_issue_id(),
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_FINGERPRINT_MISMATCH,
            translation_placeholders={
                "host": str(err.host),
                "seen_fingerprint": seen,
            },
        )

    def _clear_fingerprint_mismatch(self) -> None:
        """A socket opened AND the resync went through with the pinned certificate — whatever
        was answering with the wrong one is gone (or the user pasted the new pin)."""
        self._fingerprint_mismatch_logged = False
        ir.async_delete_issue(self.hass, DOMAIN, self._fingerprint_issue_id())

    async def _resync(self) -> None:
        """Re-fetch connections + the full snapshot and replace self.data — run on every ws
        connect to recover anything missed while disconnected. Raises on failure so the caller
        drops the connection and retries with backoff (a connected socket patching a stale
        snapshot is worse than a reconnect). If the connection set changed shape while we were
        down, this also schedules the entry reload that rebuilds the entities.
        """
        new_conns = await self.client.fetch_connections()
        raw = await self.client.fetch_state()
        data = _state_from_snapshot(raw, self.data)
        try:
            _apply_afk(data, await self.client.fetch_afk())
        except SwitchboardApiError as err:
            # Best-effort extras: a missing /api/afk must not force a reconnect loop when the
            # snapshot itself came through fine.
            _LOGGER.debug("switchboard: /api/afk unavailable: %s", err)
        self._replace_connections(new_conns)
        self.async_set_updated_data(data)

    @callback
    def _handle_frame(self, frame: dict[str, Any]) -> None:
        # Re-fire raw frame for user automations regardless of whether it touches an entity —
        # unless it is a replay too old to act on (see `_is_late`); the entities still take it.
        if self._is_late(frame):
            _LOGGER.debug("switchboard: replayed %s too late to fire on the bus", frame.get("type"))
        else:
            self.hass.bus.async_fire(EVENT_SWITCHBOARD, self._bus_frame(frame))
        if self._apply(frame) and not self._awaiting_resume:
            self.async_set_updated_data(self.data)

    def _is_late(self, frame: dict[str, Any]) -> bool:
        """A REPLAYED frame (docs/HA.md "Resume": `replayed: true`, its original `ts`) older than
        the `late_event_max_age` option. An automation reacting to it now would act minutes after
        the fact — a light flashing for a follow that happened while the link was down — so it is
        not fired; the entities still apply it (state is state). A replay whose age cannot be read
        counts as late: it IS a replay, and firing it blind is the failure this guards against.
        Live frames are never late."""
        if frame.get("replayed") is not True:
            return False
        max_age = self.entry.options.get(CONF_LATE_EVENT_MAX_AGE, DEFAULT_LATE_EVENT_MAX_AGE)
        at = _frame_epoch_secs(frame.get("ts"))
        return at is None or self._now() - at > max_age

    def _bus_frame(self, frame: dict[str, Any]) -> dict[str, Any]:
        """What actually goes on the HA bus for one frame.

        `twitch_chat_message` and `twitch_moderation` are redacted HERE, not by the app: HA's
        recorder stores every non-excluded custom event (`events` / `event_data`, kept for
        `purge_keep_days`), so re-firing them verbatim builds a per-line archive on the HA box of
        who said what, and of which viewer was moderated and why — records the app refuses to
        write to its own log (SB-D-022, SB-00060). The frames still fire (chat with
        `author`/`text` as null, the contract's own redacted shape; moderation with the viewer
        fields null) so they can be counted and reacted to; the `mirror_chat_text` and
        `mirror_moderation_targets` options opt back in.
        """
        etype = frame.get("type")
        if etype == "twitch_chat_message":
            if self.entry.options.get(CONF_MIRROR_CHAT_TEXT, False):
                return frame
            return {**frame, "author": None, "text": None}
        if etype == "twitch_moderation":
            # Same recorder reasoning for the moderated viewer: the action and outcome stay, the
            # login / user id / reason / error text naming them go, unless the user opts in.
            if self.entry.options.get(CONF_MIRROR_MODERATION_TARGETS, False):
                return frame
            return {
                **frame,
                "target_login": None,
                "target_user_id": None,
                "reason": None,
                "error": None,
            }
        return frame

    @callback
    def _apply(self, frame: dict[str, Any]) -> bool:
        """Patch self.data from one event frame. Returns True if entity state changed."""
        data = self.data
        etype = frame.get("type")

        # `connections_changed`, `update_available` and `update_ready` are contract events
        # (docs/HA.md "Full event reference", since app 2026-09-28): they keep the connection list
        # and /api/state's `update` live. Every value they touch is also carried by /api/state,
        # re-fetched on every reconnect, so a missed frame costs freshness, never correctness.
        if etype == "connections_changed":
            # Connection set may have changed → refresh list + snapshot, reload if entities differ.
            self.hass.async_create_task(self._refresh_connections())
            return False

        cid = frame.get("connection_id")
        if etype in _OBS_EVENTS:
            if etype == "stream_delay_changed" and frame.get("peer_id"):
                # A paired peer's OBS (app 2026-10-15): `connection_id` is a connection ON that
                # peer, never one of this app's instances — not ours to patch.
                return False
            inst = data.obs.get(cid)
            if inst is None:
                return False  # unknown connection; a connections_changed/reload will add it
            if etype == "obs_scene_changed":
                inst["current_scene"] = frame.get("scene")
                # A per-scene AFK override on the default instance's live scene REPLACES the
                # rule threshold while that scene is up (docs/HA.md "AFK numbers") — resample.
                self.schedule_afk_refresh()
            elif etype == "obs_connection":
                inst["connected"] = bool(frame.get("connected"))
            elif etype == "obs_stream_state":
                active = bool(frame.get("active"))
                # `restart: true` (app 2026-10-18, docs/HA.md field notes): this state belongs to
                # an automatic restart the app runs — the stream goes on, and the app keeps its
                # start. So its `stopped` must not clear the anchor and its `started` must not
                # move it, or the "stream started" sensor would reset mid-stream (SB-00250).
                restart = bool(frame.get("restart"))
                # The event carries no start timestamp; derive it on the off→on edge (the next
                # resync replaces it with the authoritative `stream_started_ms`). A restart's
                # `started` keeps the original anchor; it only derives one when none is known
                # (e.g. Home Assistant connected mid-restart).
                if active and not inst.get("streaming"):
                    if not restart or inst.get("stream_started_ms") is None:
                        inst["stream_started_ms"] = int(time.time() * 1000)
                elif not active and not restart:
                    inst["stream_started_ms"] = None
                inst["streaming"] = active
            elif etype == "obs_record_state":
                inst["recording"] = bool(frame.get("active"))
            elif etype == "obs_scenes_changed":
                inst["scenes"] = list(frame.get("scenes") or [])
            elif etype == "obs_delay_changed":
                inst["stream_delay_secs"] = frame.get("secs")  # None = delay off
            elif etype == "stream_delay_changed":
                # A scene change flipped the delay on this instance.
                inst["stream_delay_secs"] = frame.get("seconds") if frame.get("enabled") else None
            elif etype == "obs_frozen":
                # OBS itself stopped rendering/answering (or recovered) — only an OBS restart
                # fixes it; the app stops restarting the stream meanwhile.
                inst["frozen"] = bool(frame.get("frozen"))
            return True

        if etype == "machine_state_changed":
            data.afk = frame.get("state") == "afk"
            # An explicit active/afk flip can consume or moot a snooze window — resample.
            self.schedule_afk_refresh()
            return True

        if etype == "app_detect_changed":
            data.focused_app = frame.get("focused")
            data.running_apps = list(frame.get("running") or [])
            data.watched_focused = bool(frame.get("watched_focused"))
            data.watched_running = bool(frame.get("watched_running"))
            return True

        if etype == "twitch_category_updated":
            inst = data.twitch.setdefault(cid, {})
            inst["category_id"] = frame.get("game_id")
            inst["category_name"] = frame.get("game_name")
            return True

        if etype == "twitch_stream_status":
            inst = data.twitch.setdefault(cid, {})
            for k in (
                "live",
                "title",
                "category_id",
                "category_name",
                "box_art_url",
                "started_at_ms",
            ):
                inst[k] = frame.get(k)
            return True
        if etype == "twitch_chatters_updated":
            inst = data.twitch.setdefault(cid, {})
            inst["viewers"] = frame.get("watching")
            inst["chatters"] = frame.get("chatters")
            return True
        if etype == "twitch_audience_totals":
            # Follower/subscriber totals on the tracker's cadence — the same numbers /api/state's
            # twitch snaps carry as `followers`/`subs`, so this keeps the resync seed live.
            inst = data.twitch.setdefault(cid, {})
            inst["followers"] = frame.get("followers")
            inst["subs"] = frame.get("subs")
            return True

        if etype == "input_overlay_visibility":
            # The cutscene switch flipped (a deck key, a rule, the app, or its auto-show timer).
            data.input_overlay_visible = frame.get("visible") is not False
            at = frame.get("auto_show_at_ms")
            data.input_overlay_auto_show_at_ms = at if isinstance(at, int) else None
            return True

        if etype == "alert_queue_changed":
            # One alert preset's queue moved (docs/HA.md "Alert presets"); the slice was seeded
            # from /api/state. The deleted-preset frame removes the row — the platforms' listeners
            # then remove that preset's entities.
            pid = frame.get("preset_id")
            if not isinstance(pid, str) or not pid:
                return False
            if _is_deleted_alert_frame(frame):
                return data.alerts.pop(pid, None) is not None
            row = _alert_view(frame, data.alerts.get(pid))
            if data.alerts.get(pid) == row:
                return False
            data.alerts[pid] = row
            return True

        if etype == "connection_status_changed":
            # One connection-status row changed (docs/HA.md "Connection status"): the whole row,
            # replaced by key. Receiving it proves the app is new enough even if the snapshot
            # predates the slice.
            row = _status_row(frame.get("entry"))
            if row is None:
                return False
            if data.connection_status is None:
                data.connection_status = {}
            if data.connection_status.get(row["key"]) == row:
                return False
            data.connection_status[row["key"]] = row
            return True

        if etype == "connection_status_removed":
            key = frame.get("key")
            if not isinstance(key, str) or data.connection_status is None:
                return False
            return data.connection_status.pop(key, None) is not None

        if etype == "variable_changed":
            # One user variable set/incremented — or deleted (`deleted: true`, 2026-10-19; absent
            # from older apps = false), which drops the name; the full map was seeded from
            # /api/state.
            name = frame.get("name")
            if not isinstance(name, str) or not name:
                return False
            if frame.get("deleted") is True:
                return data.variables.pop(name, None) is not None
            value = frame.get("value")
            data.variables[name] = "" if value is None else str(value)
            return True

        if etype == "update_available":
            data.update = {
                "version": frame.get("version"),
                "body": frame.get("body"),
                "ready": False,
            }
            return True
        if etype == "update_ready":
            # Carry forward any previously-shown body; update_ready only signals readiness.
            data.update = {
                **(data.update or {}),
                "version": frame.get("version"),
                "ready": True,
            }
            return True

        if etype in _SPOTIFY_TRACK_EVENTS:
            return self._set_spotify(frame.get("now"), _spotify_gate(frame.get("now")))
        if etype == "spotify_playback_paused":
            return self._set_spotify(frame.get("now"), SPOTIFY_PAUSED)
        if etype == "spotify_playback_stopped":
            # No payload on this one (docs/HA.md field notes) — nothing is playing.
            return self._set_spotify(None, SPOTIFY_STOPPED)

        # Everything else on the contract's event list (home_assistant_*, overlay_alert (bare or
        # a preset's alert starting on screen — `preset_id`/`kind`/`variant_id`/`test`; the
        # queue state it implies arrives as alert_queue_changed),
        # peer_lifecycle/peer_reachability/peer_state_changed, rule_fired, the rule_action_* trio,
        # rule_events_dropped, external_command/external_command_failed, opendeck_*, twitch_event,
        # twitch_go_live, twitch_stream_target_restored, navigate_to_view, obs_reconnecting,
        # obs_scene_renamed, obs_launched_local, obs_stream_health, twitch_chat_command,
        # mesh_identity_reset, plugin_paired/removed,
        # spotify_song_liked/spotify_playlist_track_added, insights_session_ended,
        # obs_input_mute_changed, obs_sources_changed, obs_source_visibility_changed,
        # twitch_clip_created, obs_disk_space, obs_stream_stalled,
        # overlay_countdown, hotkey_pressed, twitch_chat_message, twitch_moderation,
        # twitch_moderation_paused, action_skipped, discord_routes_changed)
        # backs no entity — it is already on the HA bus as `switchboard_event` for automations.
        #
        # twitch_moderation is one moderation rule outcome (the account-age guard — ban, timeout,
        # chat modes, … or, with `dry_run: true`, what it WOULD have done). It only arrives when the
        # token holds read_events_sensitive (it names the viewer). Momentary, so no entity; the
        # storm-guard pause (twitch_moderation_paused) is the edge worth a notification.
        #
        # action_skipped is a rule/API action the app skipped instead of running (the Auto Twitch
        # account or a Discord route resolved to nothing — `reason` says which, names only):
        # momentary, so automate on the bus event (e.g. notify "no webhook for Clips on KirboAlt").
        # discord_routes_changed carries the app's Discord route/group configuration (ids and names
        # only); no entity models routes here, so the bus event is all a consumer needs.
        #
        # peer_state_changed carries a paired PEER machine's OBS snapshot (the mesh mirror); this
        # integration models one Switchboard machine per config entry, so a peer's OBS is that
        # peer's own entry. external_command_failed is the failure edge of external_command —
        # momentary, for notifications.
        #
        # The two Spotify like/playlist events deliberately DON'T touch the Spotify sensor: they
        # carry no now-playing snapshot (the liked track may already have moved on by the time the
        # frame arrives), and they are momentary actions rather than state. Automate on them with
        # `event_type: switchboard_event` + `event_data.type`, branching on the `liked`/`added`
        # boolean, which carries the direction.
        #
        # obs_stream_health is event-only for now: it IS a persisting state, but /api/state carries
        # no health field, so an entity would have nothing to hydrate from after a restart and
        # would sit at an invented value until the next verdict change. Automate on the event.
        #
        # insights_session_ended likewise backs no entity: it is a one-shot summary of a session
        # that has just ENDED, not current state, so a sensor holding it would report stale numbers
        # for however long until the next stream. Its whole payload rides the bus event, which is
        # what an automation wants (post a recap, log the numbers, drive a notification).
        # twitch_clip_created is a one-shot: a clip was just cut, and its links (`url` once Twitch
        # finishes processing, `edit_url` immediately) are what an automation wants to post. It is
        # not state, so no entity.
        #
        # hotkey_pressed says a bound global hotkey was pressed. Momentary by definition, so no
        # entity; automate on the event if you want a keystroke on this machine to drive HA.
        #
        # overlay_countdown says the countdown overlay's target moved (or was cleared). The timer
        # runs in the overlay page, not here, so there is no state to hold — and a sensor counting
        # down would mean a per-second update for a number HA can derive from the target.
        #
        # obs_disk_space reports free space on OBS's recording drive crossing the configured
        # floor. Same reasoning as obs_stream_health: it is state, but /api/state carries no disk
        # field, so an entity would sit at an invented value after a restart. Automate on the
        # event — "recording disk low" is exactly the notification HA is good at.
        #
        # obs_stream_stalled says a live OBS output sent nothing for ~15 s and the app restarted
        # it (`restarted: true`) or gave up (`restarted: false`). Momentary by definition — the
        # restart already happened — so no entity; the give-up edge is the one to notify on.
        #
        # obs_input_mute_changed says an OBS audio input was muted/unmuted, and
        # obs_source_visibility_changed that a source became shown/hidden on an instance's program
        # output; obs_sources_changed replaces an instance's whole source list. Since 2026-09-26
        # /api/state carries those rows (`obs_sources`, the OpenDeck source tiles' state), so the
        # state now hydrates — but an entity PER source would mean creating and removing HA
        # entities at runtime as sources are added and removed in OBS, the same bigger product
        # call as per-variable entities below. The rows are tolerated, not mirrored; automate on
        # the events.
        #
        # (variable_changed IS applied above — it patches `data.variables`, seeded from
        # /api/state, so a template sensor can read it — but a dedicated entity PER variable would
        # mean creating and removing HA entities at runtime as the user adds and deletes them,
        # which is a bigger product call than this contract sync.)
        return False

    @callback
    def _set_spotify(self, now: dict[str, Any] | None, gate: str) -> bool:
        """Store a now-playing payload + gate. Returns True only when something we EXPOSE
        changed: `spotify_now_playing` is re-emitted on position drift, so patching blindly
        would rewrite the sensor (and its history) every few seconds for no visible change."""
        view = _spotify_view(now)
        if view == self.data.spotify_now and gate == self.data.spotify:
            return False
        self.data.spotify_now = view
        self.data.spotify = gate
        return True

    @callback
    def schedule_afk_refresh(self) -> None:
        """Resample `GET /api/afk` soon, coalescing bursts into one request.

        Called when something that can move `threshold_secs`/`snooze_secs` happened (scene
        change, machine-state flip, one of our own AFK service calls). There is no clock: the
        live `idle_secs` countdown is not mirrored (see `SwitchboardData`).
        """
        if self._closing:
            return
        if self._afk_task and not self._afk_task.done():
            self._afk_again = True
            return
        self._afk_task = self.hass.async_create_task(self._refresh_afk())

    async def _refresh_afk(self) -> None:
        while True:
            self._afk_again = False
            await asyncio.sleep(AFK_COALESCE_SECS)
            if self._closing:
                return
            try:
                raw = await self.client.fetch_afk()
            except SwitchboardApiError as err:
                _LOGGER.debug("switchboard: /api/afk refresh failed: %s", err)
                return
            if _apply_afk(self.data, raw):
                self.async_set_updated_data(self.data)
            if not self._afk_again:
                return

    def _entity_shape(self) -> set[tuple[str, str]]:
        """(id, label) of every connection that backs entities — labels included so a rename
        (which feeds device names) triggers a rebuild, not just an id-set change."""
        return {
            (c["id"], c.get("label", c["id"]))
            for c in self.connections
            if c["integration"] in ("obs", "twitch")
        }

    def _replace_connections(self, new_conns: list[dict[str, Any]]) -> bool:
        """Swap in a fresh connection list; if the entity-backing shape changed
        (added/removed/renamed OBS or Twitch connection), entities and device names must be
        (re)built — schedule an entry reload. Returns True when a reload was scheduled.
        """
        before = self._entity_shape()
        self.connections = new_conns
        if before == self._entity_shape():
            return False
        self.hass.async_create_task(self.hass.config_entries.async_reload(self.entry.entry_id))
        return True

    async def _refresh_connections(self) -> None:
        if self._refreshing:
            # A refresh is already in flight — flag it to run once more so a connections_changed
            # that lands AFTER its fetch resolved isn't dropped. Avoids overlapping async_reload
            # calls racing the same entry when events arrive in bursts.
            self._refresh_again = True
            return
        self._refreshing = True
        try:
            while True:
                self._refresh_again = False
                try:
                    new_conns = await self.client.fetch_connections()
                except SwitchboardApiError as err:
                    _LOGGER.debug("switchboard: connection refresh failed: %s", err)
                    return
                previous = self.connections
                if self._replace_connections(new_conns):
                    # Entry reload scheduled (which also cancels this task's owner).
                    return
                if new_conns != previous:
                    # Same entity shape, different rows — a `needs_reauth` flip is exactly this
                    # case, and the entity reading it would otherwise sit stale until the next
                    # reconnect resync.
                    self.async_update_listeners()
                if not self._refresh_again:
                    return
        finally:
            self._refreshing = False
