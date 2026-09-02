# Switchboard — Home Assistant integration

A [Home Assistant](https://www.home-assistant.io/) custom integration for
[Switchboard](https://gitlab.com/KirboDev/agentic-coding/switchboard), the cross-platform streaming
control hub (OBS, Twitch, Spotify, Discord, with an event→action rules engine).

It consumes Switchboard's **External API** (the same authenticated surface documented in the app's
`docs/HA.md`): it streams every Switchboard event onto the HA bus, exposes OBS/Spotify/AFK state as
entities, and lets HA drive Switchboard actions through services.

> **Local push.** State arrives over a long-lived websocket — no polling. The integration fetches a
> snapshot on every (re)connect, then stays live from the event stream.

## What you get

**Entities** (one device per OBS / Twitch connection, plus a Switchboard service device):

- **Binary sensors** — per OBS connection: `Connected`, `Streaming`, `Recording`; per Twitch
  connection: `Live`; global: `AFK` (attributes: `threshold_secs` — how long idle before the
  idle→AFK automation fires, with any per-scene override applied, `null` = never — and
  `snooze_until`), `Watched app active` (app-detection — a watched app is focused *or* running),
  `Update available`, `Connection needs re-authentication` (a diagnostic **problem** sensor: on
  while any Switchboard connection's own credential was rejected by its provider — a revoked
  Twitch refresh token, a Home Assistant long-lived token answered with `auth_invalid`. That is
  not "offline": the app has stopped retrying and nothing reconnects until someone replaces the
  credential in Switchboard. Attributes name the affected `connections` and `integrations`).
- **Sensors** — per OBS connection: current `Scene` (attribute: that instance's `scenes` list),
  `Stream started` (timestamp — derive uptime from it) and `Stream delay` (seconds, `0` = off);
  per Twitch connection: `Viewers`, `Chatters`, `Followers`, `Subscribers` (the last two are the
  app's audience-tracker totals — unknown until it has run once), `Category` (attributes: category
  id, box-art URL, title, `started_at_ms`) and `Live since` (timestamp); global: `Spotify` playback
  (`playing`/`paused`/`stopped`) with now-playing attributes (title, artist, featuring, album,
  playlist, playlist URL, art URL, up-next track, duration), `Focused app` (attributes: the
  `running` watch-list ids plus the separate `watched_focused` / `watched_running` flags), and
  `Version` (with any pending-update attributes).

**Services** (→ `POST /api/command`):

- `switchboard.run_action` — generic passthrough for **any** action (`action_type`, `target`,
  `value`, `action_params`); forward-compatible with new Switchboard actions.
- `switchboard.obs_scene_set` — switch an OBS connection's program scene.
- `switchboard.go_live` — Twitch **go-live composite**: fetches the account's stream key, sets it
  on the target OBS **and starts its stream** as one operation, then makes that account
  Switchboard's default. `account_id` is the Twitch connection (an account on the Switchboard
  machine — it owns the key); `obs_id` optionally picks the OBS connection the key is delivered
  to (blank = Switchboard's default OBS). The stream key itself never appears in any request,
  response or event — the payload is ids only. Needs a token with the **`twitch_control`** scope:
  the global External API token always has it, so a `403` means a paired-plugin token without the
  scope — grant `twitch_control` to that plugin in Switchboard, or use the global token. After
  the stream stops, Switchboard auto-restores the OBS stream settings and fires
  `twitch_stream_target_restored`.
- `switchboard.overlay_alert` — show an alert on the Switchboard alerts overlay.
- `switchboard.set_variable` / `switchboard.add_to_variable` — write one of Switchboard's user
  variables (counters, mode flags, labels). `add_to_variable` takes an `amount` (default `1`,
  negative subtracts) and starts a missing variable at 0, so an HA automation can keep a tally
  without any setup in Switchboard. Every variable's current value is on the coordinator data as
  `variables` (from `/api/state`) and each change arrives as a `switchboard_event` of type
  `variable_changed`.
- `switchboard.set_machine_state` — set Switchboard's machine state (`afk` / `active`), e.g. from an
  HA presence automation; fires Switchboard's `machine_state_*` triggers so it runs its own AFK
  automations (and flips back to active on input).
- `switchboard.afk_snooze` — hold off the idle→AFK automation for N seconds (mid-cutscene guard).
  Repeated calls stack (capped at 4 h); `0` cancels.
- `switchboard.afk_reset_idle` — "I'm still here": reset Switchboard's idle clock without
  synthesizing input.
- `switchboard.light_flash` — blink a light or light group N times in a colour through
  *Switchboard's own* downstream Home Assistant connection, then restore its previous state.
  Like every `ha_*` action this needs the **global** External API token, not a paired-plugin one.

`target` accepts a friendly connection **label** or its **id** (anything that isn't a known
connection — e.g. the `spotify` sentinel — is passed through unchanged). Every service also takes an
optional `entry_id` to address a specific instance when several Switchboard machines are configured
(leave blank with a single instance).

**Bus event** — **every** Switchboard event is re-fired as `switchboard_event` (with its raw `type`
and fields), including the ones that back no entity: `twitch_event` (follows/subs/raids/cheers),
`rule_fired`, the peer-action queue trio `rule_action_queued` / `_delivered` / `_failed`,
`rule_events_dropped`, `peer_lifecycle` / `peer_reachability` / `peer_state_changed`, `overlay_alert`,
`home_assistant_state_changed` (Switchboard's *own* watched HA entities), `external_command` /
`external_command_failed`, `twitch_audience_totals`,
`opendeck_event`, `mesh_identity_reset`, the go-live pair `twitch_go_live` /
`twitch_stream_target_restored`, and the rest of the contract's event vocabulary.

> **Two of those arrive redacted without `read_events_sensitive`.** `rule_fired.value` and
> `overlay_alert.text` are rendered from whatever triggered the rule — a viewer's name, a chat
> message, an HA entity's state — so a token without that scope receives the *same frame* with
> that one field as an **empty string**. Nothing else changes: same event type, same keys, nothing
> absent. An automation that puts `event_data.value` into a notification should skip the
> notification when it is empty rather than send a blank one. With the global token (the
> recommended one, see below) both arrive populated.

```yaml
automation:
  - alias: Announce raids
    trigger:
      - platform: event
        event_type: switchboard_event
        event_data:
          type: twitch_event
          kind: twitch_raid
    action:
      - service: switchboard.overlay_alert
        data:
          text: "Incoming raid!"
```

## Requirements

- Switchboard running with the **External API enabled** — generate a token in
  **Settings → External API** (shown once).
- The **Events** and **Control** IP ACLs (`events_access_*` / `control_access_*`) must allow your
  Home Assistant host. They default to **local-only**, so if HA runs on a different machine than
  Switchboard, add HA's IP to both ACLs in **Settings → External API** — otherwise the websocket
  (Events) and service calls (Control) are rejected with `403`.

## Installation

### HACS (recommended)

HACS → ⋮ → **Custom repositories** → add `https://github.com/Kirbo/switchboard-ha` with category
**Integration** → install **Switchboard** → restart Home Assistant.

### Manual

Copy `custom_components/switchboard` into your HA `config/custom_components/` directory and restart.

## Configuration

**Settings → Devices & Services → Add Integration → Switchboard**, then provide:

| Field | Notes |
|---|---|
| **Host** | Switchboard machine's hostname or IP. |
| **Port** | `38474` (the TLS mesh/API port) unless changed. |
| **API token** | The External API bearer token from Switchboard. |
| **Verify TLS certificate** | Leave **off** for Switchboard's self-signed cert, and pin the fingerprint below. Turn it **on** only if you front the app with a certificate your Home Assistant trusts. |
| **TLS fingerprint** | **Required** unless verification is on. SHA-256 fingerprint to pin, shown on Switchboard's Peers tab — it authenticates the self-signed cert instead of skipping verification. |

> **One of the two is mandatory.** With verification off *and* no fingerprint, nothing authenticates
> the connection: any device that can ARP-spoof your network terminates it with its own certificate
> and reads the API token off the first request — a token that can drive OBS, post to your Twitch
> chat and call Home Assistant services. The config flow refuses that combination, and since it used
> to be the shipped default, an **existing** entry configured that way now stops loading and raises a
> repair telling you to reconfigure. Paste the fingerprint and it starts again.

> **Token kind.** Paste the **global** External API token (Settings → External API). The per-plugin
> token from Switchboard's pairing Grant flow also authenticates, but it is scope-limited: the
> `ha_*` actions behind `switchboard.light_flash` are global-token-only, so pairing would give you
> a strictly weaker integration. There is deliberately no pairing step in this config flow.
>
> If you *do* use a paired token, this integration needs the **`read_events_sensitive`** scope as
> well as `read_state`. Switchboard splits those deliberately: `read_state` is the rig (OBS, Spotify,
> AFK, stream status), while `read_events_sensitive` covers the events that name your viewers or
> describe your home — `twitch_event`, `twitch_chat_command`, `home_assistant_state_changed`,
> `app_detect_changed`, `hotkey_pressed` (plus the app's own diagnostics frames
> `home_assistant_flash_issue` / `home_assistant_flash_restored` and `plugin_pair_requested`, which
> back no entity here). This integration mirrors those onto the HA bus, so without that scope the
> corresponding sensors and event triggers simply never fire, and `rule_fired` / `overlay_alert`
> arrive with their text emptied (see the bus-event note above). Tokens paired before the split
> keep it automatically; new ones need it ticked on the plugin's card in Switchboard.

## Notes

- **Commands are rate-limited by Switchboard**, not by this integration: every caller gets a write
  budget of 10 commands per second (burst 30) shared across `POST /api/command` and `POST /api/event`,
  and over it the app answers `429`. That is a "slow down", never an outage or a bad token — the
  budget refills continuously, so the client waits and re-sends (up to four attempts over ~1.75 s)
  before the service call fails, and the events websocket and entities are untouched either way.
  Nothing driven at human pace will ever see it; a script looping over a dozen actions might, and
  should space them out.
- The API event/command schema is **additive**: new event types and fields appear over time and are
  ignored if unknown. New OBS/Twitch connections added (or renamed) in Switchboard trigger a reload
  so their entities and device names follow.
- A few numbers are derived locally rather than streamed, exactly as the contract prescribes:
  **stream/live uptime** comes from the `Stream started` / `Live since` timestamps, and album/box
  art is loaded from the `art_url` / `box_art_url` attributes (the app broadcasts URLs, never
  images).
- The live AFK **countdown** (`idle_secs` / `afk_in_secs`) is not mirrored into an entity — it
  changes every second and would rewrite the sensor's history once a second. The AFK binary sensor
  carries the stable halves (`threshold_secs`, `snooze_until`); poll `GET /api/afk` yourself if you
  want a ticking countdown.
- This integration lives in its own repo (not the Switchboard monorepo) because HACS only installs
  from GitHub. The API contract it targets is documented in the app's `docs/HA.md`.

## Development

The toolchain is pinned in `mise.toml` ([mise](https://mise.jdx.dev/)); Home Assistant itself is a
pip package, provisioned into `.venv` by the test task.

```bash
mise install
mise run lint   # ruff check + format --check
mise run test   # pytest against a real Home Assistant (provisions .venv first)
```

`tests/test_contract.py` mirrors the app contract's **Full event reference** and `/api/state`
example frame-for-frame: when `docs/HA.md` gains or renames something, update that file first and
let the suite point at everything downstream that still reads the old shape.

## Related

- **[Switchboard](https://gitlab.com/KirboDev/agentic-coding/switchboard)** — the desktop app this
  integration controls (OBS / Twitch / Spotify / Discord + an event→action rules engine). The `/api`
  contract is [`docs/HA.md`](https://gitlab.com/KirboDev/agentic-coding/switchboard/-/blob/main/docs/HA.md).
- **[OpenDeck plugin](https://gitlab.com/KirboDev/agentic-coding/opendeck/opendeck-switchboard)** — the
  sibling plugin: live Switchboard state on a Stream Deck / OpenDeck, plus command keys. Same `/api`
  surface, a different consumer.

## License

MIT
