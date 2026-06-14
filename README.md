# Switchboard — Home Assistant integration

A [Home Assistant](https://www.home-assistant.io/) custom integration for
[Switchboard](https://gitlab.com/KirboDev/agentic-coding/switchboard), the cross-platform streaming
control hub (OBS, Twitch, Spotify, Discord, with an event→action rules engine).

It consumes Switchboard's **External API** (the same authenticated surface documented in the app's
`docs/HA.md`): it streams every Switchboard event onto the HA bus, exposes OBS/Spotify/AFK state as
entities, and lets HA drive Switchboard actions through services.

> **Local push.** State arrives over a long-lived websocket — no polling. The integration fetches a
> snapshot once on connect, then stays live from the event stream.

## What you get

**Entities** (one device per OBS connection, plus a Switchboard service device):

- **Binary sensors** — per OBS connection: `connected`, `streaming`, `recording`; global: `AFK`.
- **Sensors** — per OBS connection: current `Scene`; global: `Spotify` playback
  (`playing`/`paused`/`stopped`) with now-playing attributes (title, artist, album, …).

**Services** (→ `POST /api/command`):

- `switchboard.run_action` — generic passthrough for **any** action (`action_type`, `target`,
  `value`, `action_params`); forward-compatible with new Switchboard actions.
- `switchboard.obs_scene_set` — switch an OBS connection's program scene.
- `switchboard.overlay_alert` — show an alert on the Switchboard alerts overlay.

`target` accepts a friendly connection **label** or its **id** (anything that isn't a known
connection — e.g. the `spotify` sentinel — is passed through unchanged).

**Bus event** — every Switchboard event is re-fired as `switchboard_event` (with its raw `type` and
fields), so you can trigger HA automations on `twitch_event`, `rule_fired`, `obs_scene_changed`, etc.

```yaml
automation:
  - alias: Announce raids
    trigger:
      - platform: event
        event_type: switchboard_event
        event_data:
          type: twitch_event
          kind: raid
    action:
      - service: switchboard.overlay_alert
        data:
          text: "Incoming raid!"
```

## Requirements

- Switchboard running with the **External API enabled** — generate a token in
  **Settings → External API** (shown once).
- The **Events** and **Control** IP ACLs must allow your Home Assistant host. They default to
  **local-only**, so if HA runs on a different machine than Switchboard, add HA's IP to the
  whitelist (Settings → External API for Events; Peers tab for Control) — otherwise the websocket
  and service calls are rejected with `403`.

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
| **Verify TLS certificate** | Leave **off** for the self-signed cert (default) unless you front it with a trusted certificate. |
| **TLS fingerprint** | *Optional, recommended.* SHA-256 fingerprint to pin (shown on Switchboard's Peers tab) — pins the self-signed cert instead of skipping verification. |

## Notes

- The API event/command schema is **additive**: new event types and fields appear over time and are
  ignored if unknown. New OBS connections added in Switchboard trigger a reload so their entities
  appear.
- This integration lives in its own repo (not the Switchboard monorepo) because HACS only installs
  from GitHub. The API contract it targets is documented in the app's `docs/HA.md`.

## License

MIT
