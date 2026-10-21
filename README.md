# Switchboard — Home Assistant integration

A [Home Assistant](https://www.home-assistant.io/) custom integration for
[Switchboard](https://gitlab.com/KirboDev/agentic-coding/switchboard), the cross-platform streaming
control hub (OBS, Twitch, Spotify, Discord, with an event→action rules engine).

It consumes Switchboard's **External API** (the same authenticated surface documented in the app's
`docs/HA.md`): it streams every Switchboard event onto the HA bus, exposes OBS/Spotify/AFK state as
entities, and lets HA drive Switchboard actions through services.

> **Local push.** State arrives over a long-lived websocket — no polling. The integration fetches a
> snapshot on every (re)connect — and again whenever the app reports that events were dropped on
> the way — then stays live from the event stream.

## What you get

**Entities** (one device per OBS / Twitch connection, plus a Switchboard service device):

- **Binary sensors** — per OBS connection: `Connected`, `Streaming`, `Recording`, `Frozen` (a problem sensor: OBS itself stopped rendering or answering — restart OBS); per Twitch
  connection: `Live`; global: `AFK` (attributes: `threshold_secs` — how long idle before the
  idle→AFK automation fires, with any per-scene override applied, `null` = never — and
  `snooze_until`), `Input overlays visible` (the keyboard/mouse/gamepad overlays are shown on
  the stream — off while hidden by the cutscene switch; attribute `auto_show_at` = when
  Switchboard's auto-show timer brings them back), `Watched app active` (app-detection — a
  watched app is focused *or* running),
  `Update available`, `Connection needs re-authentication` (a diagnostic **problem** sensor: on
  while any Switchboard connection's own credential was rejected by its provider — a revoked
  Twitch refresh token, a Home Assistant long-lived token answered with `auth_invalid`. That is
  not "offline": the app has stopped retrying and nothing reconnects until someone replaces the
  credential in Switchboard. Attributes name the affected `connections` and `integrations`);
  per **alert preset** (Switchboard 2026-10-15 or newer): `<preset> paused` — the preset's alert
  queue is held by hand (`switchboard.alert_pause`, a deck key, a rule or the app's editor);
  **Discord voice** (Switchboard 2026-10-21 or newer, with its Discord voice controls connected):
  `Discord muted`, `Discord deafened` and `In Discord voice` — the Discord desktop app on
  Switchboard's machine (attributes while in voice: `call_kind` — `server` / `dm` / `group_dm` —
  and `channel_id`, plus the `channel` and `guild` names only when the token holds Switchboard's
  `read_events_sensitive` scope; the global token does).
- **Sensors** — per OBS connection: current `Scene` (attribute: that instance's `scenes` list),
  `Stream started` (timestamp — derive uptime from it; an automatic restart the app runs keeps
  it) and `Stream delay` (seconds, `0` = off);
  per Twitch connection: `Viewers`, `Chatters`, `Followers`, `Subscribers` (the last two are the
  app's audience-tracker totals — unknown until it has run once), `Category` (attributes: category
  id, box-art URL, title, `started_at_ms`) and `Live since` (timestamp); global: `Spotify` playback
  (`playing`/`paused`/`stopped`) with now-playing attributes (title, artist, featuring, album,
  playlist, playlist URL, art URL, up-next track, duration), `Focused app` (attributes: the
  `running` watch-list ids plus the separate `watched_focused` / `watched_running` flags), and
  `Version` (with any pending-update attributes), `Discord input mode` (`voice_activity` /
  `push_to_talk`; Switchboard 2026-10-21 or newer); per **alert preset**: `<preset> queued` — how
  many alerts are waiting (attributes: `showing` = the alert kind on screen right now, e.g.
  `twitch_follow`, `null` = nothing; `dropped` = alerts thrown away since the app started;
  `merged` = alerts folded into a storm summary since the app started; `paused`; `preset_id`);
  `Connection status` (Switchboard 2026-10-18 or newer; diagnostic) — the worst state of every
  connection, subscription and background task the app runs (`connected` while all are fine;
  `connecting` / `reconnecting` / `down` / `crashed`; `disabled` when everything reporting is
  off; unknown against an older app), live from the app's push. Attributes: `problems` (how many
  need attention), `by_integration` (each integration's worst state, e.g. `obs: reconnecting`) and
  `rows` (every row with its state, the session's `drops` / `reconnects` / `crashes` and the last
  error, and `counted`) — e.g. notify when `state` becomes `down` or `crashed`. A row the app marks
  not counted (a peer whose "Counts toward Ready to stream and attention" is off, like a test VM
  that is normally powered off; Switchboard 2026-10-19 or newer) is listed in `rows` but never
  raises the state, `problems` or `by_integration`.

The four Discord entities sit on the Switchboard service device (one Discord client per
Switchboard machine) and are **unavailable** whenever Switchboard has no live session with Discord
— Discord not running, the voice controls not set up or needing a sign-in, or an older Switchboard
— rather than showing a guessed "not muted". They come back by themselves when Discord starts.
Drive Discord from Home Assistant with `switchboard.run_action` (`discord_voice_mute_toggle`,
`discord_voice_deafen`, `discord_voice_mode_set` with `value: push_to_talk`, …), and the system
output volume with the `machine_audio_*` actions (Switchboard's `docs/HA.md` lists them).

Alert-preset entities sit on the Switchboard service device and follow the app live: a preset
created in Switchboard gets its two entities on its first queue change, a deleted one has them
removed (no reload), and a rename updates the friendly name. An older Switchboard without alert
presets simply gets none.

**Services** (→ `POST /api/command`):

- `switchboard.run_action` — generic passthrough for **any** action (`action_type`, `target`,
  `value`, `action_params`); forward-compatible with new Switchboard actions. An optional
  `target_peer_id` (a paired peer's id from Switchboard's `/api/peers`) runs the action on that
  peer machine over the encrypted mesh — `target` is then a connection id on the peer.
  **Auto account + Discord routes** (Switchboard 2026-10 or newer): `target: auto` on a Twitch
  action is the **Auto** account — whichever account is live on the machine running it (a
  service call has no trigger to name one; Auto never falls back to an offline default account;
  sent verbatim, never matched against labels); `multi_live` (`default` / `skip` / `all`) says what it
  does when several are live (`default` = the default account if it is live, else skip). For
  `discord_webhook_send`, leave `target` blank and set `discord_route` (a route name, e.g.
  `Clips`): Switchboard posts to the webhook the Twitch account's Discord group maps that route to,
  with `twitch_account` (label or id; blank / `auto` = the Auto account) picking the account. These
  fields are merged into `action_params` under the contract's keys (`discord_route`,
  `twitch_account`, `multi_live`), which can also be passed there directly. Anything that resolves
  to nothing is skipped, never posted elsewhere, and fires an `action_skipped` event
  (`switchboard_event` with `type: action_skipped`, plus `reason`, `action_type`, `account`,
  `route`).
- `switchboard.obs_scene_set` — switch an OBS connection's program scene.
- `switchboard.apply_scene_stream_info` — re-apply the Twitch Stream Info (title, category, language, tags) mapped to an OBS connection's current scene, as a scene switch would (a scene set to take its Stream Info from another OBS uses that OBS's current scene).
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
- `switchboard.alert_skip` / `alert_pause` / `alert_resume` / `alert_replay` — control an alert
  preset's queue: skip ends the alert on screen now; pause holds the queue (the alert on screen
  finishes, the rest wait); resume releases it; replay queues the last alert that showed again.
  `preset` is the preset's name (case-insensitive) or id; blank = **every** preset. Needs
  Switchboard 2026-10-15 or newer (an older app, or an unknown preset, is refused with an error
  saying so). To fire a test alert, use `run_action` with `action_type: overlay_alert_test`
  (`value` = the preset; optional `action_params.alert_kind` / `alert_variant`).
- `switchboard.set_variable` / `switchboard.add_to_variable` — write one of Switchboard's user
  variables (counters, mode flags, labels). `add_to_variable` takes an `amount` (default `1`,
  negative subtracts) and starts a missing variable at 0, so an HA automation can keep a tally
  without any setup in Switchboard. Every variable's current value is on the coordinator data as
  `variables` (from `/api/state`) and each change arrives as a `switchboard_event` of type
  `variable_changed` — a deleted variable is one with `deleted: true`, and leaves `variables`.
- `switchboard.set_machine_state` — set Switchboard's machine state (`afk` / `active`), e.g. from an
  HA presence automation; fires Switchboard's `machine_state_*` triggers so it runs its own AFK
  automations (and flips back to active on input).
- `switchboard.afk_snooze` — hold off the idle→AFK automation for N seconds (mid-cutscene guard).
  Repeated calls stack (capped at 4 h); `0` cancels.
- `switchboard.afk_reset_idle` — "I'm still here": reset Switchboard's idle clock without
  synthesizing input.
- `switchboard.set_input_overlay_visible` — the cutscene switch: `visible: "false"` hides every
  input overlay (keyboard, mouse, gamepad) on the stream, `"true"` shows them, `"toggle"`
  (default) flips. Needs a Switchboard build from 2026-09-19 or newer; the `Input overlays
  visible` binary sensor mirrors the result.
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
`rule_events_dropped`, `peer_lifecycle` / `peer_reachability` / `peer_state_changed`, `overlay_alert`
(an alert preset's alert starting on screen also carries `preset_id`, `kind`, `variant_id` and
`test`), `alert_queue_changed` (what keeps the alert-preset entities current),
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

> **Fingerprint mismatch.** If the host ever presents a certificate that does *not* match the pinned
> fingerprint — Switchboard's identity was rotated (a reinstall, a mesh identity reset), or another
> device on your network is answering in its place — the integration logs one warning per outage
> with the fingerprint it saw, raises a repair, and keeps retrying with the *pinned* one. It never
> adopts a new fingerprint on its own. If you rotated it yourself, **Reconfigure** and paste the new
> fingerprint from the Peers tab; if you didn't, treat the repair as a warning.

> **Rejected token.** If Switchboard answers `401` (the token was revoked, rotated or disabled), the
> entities go unavailable, HA starts **re-authentication** for the entry, and the integration stops
> reconnecting — a dead token is not retried. Finish the re-authentication with a valid token (the
> same one, if you re-enabled it) and the entry reloads and reconnects.

### Options

**Settings → Devices & Services → Switchboard → Configure** (the gear on the entry):

| Option | Default | Notes |
|---|---|---|
| **Mirror chat text onto the event bus** | off | With it off, `twitch_chat_message` still reaches the HA bus for every chat line — so chat can be counted — but with `author` and `text` set to `null`. Turn it on to receive the names and the words (they still need the `read_events_sensitive` scope and the app's `full` chat-detail setting). |
| **Mirror moderated viewers onto the event bus** | off | With it off, `twitch_moderation` still reaches the HA bus with the action and outcome, but with `target_login`, `target_user_id`, `reason` and `error` set to `null`. Turn it on to receive who was moderated (still needs the `read_events_sensitive` scope). |
| **Fire replayed events up to this age (seconds)** | 60 | After a dropped connection, Switchboard replays the events Home Assistant missed (see *Staying connected* below). Every replayed event updates the entities, but one older than this is **not** fired as a `switchboard_event`, so an automation never reacts minutes after the fact. `0` = never fire a replayed event. |
| **Allow non-admin Home Assistant users to call Switchboard services** | off | With it off, only Home Assistant **administrators** can call the `switchboard.*` services; a non-admin user gets *Unauthorized*. Automations and other calls with no user behind them always run. See the security note below. |

> **Why off by default.** Home Assistant's recorder stores every custom event that isn't excluded
> (`events` / `event_data`, kept for `purge_keep_days`, 10 by default). Re-firing chat verbatim would
> build a per-line archive of who said what on your HA box — the archive Switchboard itself refuses
> to write to its own log under any setting. The same is true of everything else this integration
> re-fires: `twitch_chat_command`, the resub/cheer messages inside `twitch_event`, `rule_fired`
> values. If you want *nothing* from Switchboard archived, exclude the event type in your
> `configuration.yaml` — automations still trigger on it, only the recorder ignores it:
>
> ```yaml
> recorder:
>   exclude:
>     event_types:
>       - switchboard_event
> ```

> **Who can call the services.** Every `switchboard.*` service runs with this entry's Switchboard
> API token — normally the global one, which can run **any** Switchboard action, including
> `ha_service_call` back into Home Assistant with the Home Assistant token Switchboard holds. So the
> services are **admin-only by default**, the same rule Home Assistant applies to its own admin
> services: a call with no user (an automation, the system) runs, an admin's call runs, and any other
> user's call — a household member's account, a wall tablet — is refused with *Unauthorized*. A
> script inherits the user who started it. Turn on **Allow non-admin Home Assistant users to call
> Switchboard services** only if every user on this Home Assistant should be able to run Switchboard
> actions. The option is read on every call (no reload). With several Switchboard instances, a call
> that names an `entry_id` needs the option on that instance; a call that doesn't can reach any of
> them, so it needs the option on **every** instance. In Switchboard, create its Home Assistant
> long-lived token on a dedicated **non-admin** Home Assistant user, so `ha_service_call` cannot reach
> Home Assistant's admin-only services either (the app then misses Home Assistant's own
> `homeassistant_stop` signal, which only admins may subscribe to; this integration's goodbye frame
> still tells it a restart is coming).

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
> arrive with their text emptied (see the bus-event note above). `twitch_chat_message` behaves the
> same way as those two rather than vanishing: it always reaches the HA bus so chat can be counted,
> but without the scope its `author` and `text` are `null` — and this integration nulls them itself
> unless the **Mirror chat text** option is on (see Options). Tokens paired before the split keep the
> scope automatically; new ones need it ticked on the plugin's card in Switchboard.

## Staying connected

The integration recovers on its own from a Switchboard restart or update, its PC sleeping and
waking, a network flap, or Home Assistant restarting — nothing to reload.

- **Reconnect forever.** The events websocket redials on a ladder of 1, 2, 3, 5, 10, 15, 30, 45 and
  then every 60 s, each wait spread ±20 % so a restarting app is not redialled by every client at
  once. The ladder only resets once a connect is fully synced, so an app that accepts the socket
  but fails its reads is not hammered.
- **Dead links are noticed.** A ping every 30 s closes a socket left unanswered for 15 s — a link
  that died without saying so (a sleeping PC, a dropped Wi-Fi) otherwise looks healthy forever. An
  upgrade the app never answers gives up after 10 s.
- **Unavailable while down.** Every entity goes `unavailable` the moment the link drops, never
  frozen at a stale value, and comes back once the link is synced again.
- **Resume, not a gap.** Against a Switchboard that numbers its events (`seq` / `boot_id`,
  docs/HA.md "Resume"), a reconnect asks the app to replay what Home Assistant missed — the
  entities then end exactly where they would have without the drop, and `/api/state` is not
  re-read. Each replayed event is applied **once** (one already seen before the drop is skipped,
  so nothing fires twice). Replayed events are marked `replayed: true` with their original `ts`.
  **Late events update sensors but do not trigger automations:** a replayed event older than the
  *Fire replayed events up to this age* option (60 s by default) moves the entities but is not
  fired as a `switchboard_event` — a light flashing for a follow that happened five minutes ago,
  while the link was down, is wrong. When the app cannot replay (too far back, or it restarted) it
  says so and the integration re-reads the full snapshot. Nothing is written to disk: an entry
  reload or a Home Assistant restart re-reads the snapshot, as does every reconnect to an older
  Switchboard.
- **What needs you.** A revoked token (`401`) starts re-authentication and stops redialling (a
  dead token cannot come back by retrying); a certificate that no longer matches the pin raises a
  repairs issue and keeps redialling without adopting it (see *Configuration*).

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
- **Restarting Home Assistant is not an outage.** When Home Assistant stops, the integration tells
  Switchboard so on its open events websocket (a `goodbye` frame with reason `restart` or `stop`;
  Switchboard versions that predate it ignore the frame): the app then treats the drop of its own
  Home Assistant connection as an expected restart — INFO lines instead of warnings, no
  "reconnecting" rows, the connection card stays connected — for its restart grace window (60 s by
  default, up to 5 minutes, in Switchboard's Settings → Home Assistant), and one "reconnected after
  its restart" row once Home Assistant is back. The frame is fire-and-forget (capped at 1 s, never delays the shutdown), and the
  integration neither retries nor warns while Home Assistant goes down; it reconnects on the next
  start. A restart is recognised from Home Assistant's restart exit code or a
  `homeassistant.restart` service call in the two minutes before the stop (on HA OS / Supervised
  the Supervisor restarts Core with a plain stop, so the service call is what tells them apart);
  anything else — e.g. `ha core restart` from the Supervisor CLI — is reported as `stop`, which the
  app's grace covers the same way.
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
