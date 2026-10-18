"""Resume after a drop (docs/HA.md "Resume"): the app replays what this client missed.

Every event frame from a resume-capable app carries `seq` + `boot_id`. A reconnect asks for
`?resume=<last seq>&boot=<boot_id>`; the app replays the missed frames (`replayed: true`, their
original `ts`), then sends `resumed` — or `gap` when it cannot, and the client re-seeds. These tests
drive the real `_ws_loop` with the scripted sessions of test_push.py and pin:

- the position each reconnect asks for, and none on the first connect or against an older app;
- a replay that overlaps what was already seen is applied once — no sensor or bus event twice;
- `resumed` brings the entities back without a re-read, `gap` re-seeds, and a restarted app's new
  sequence is adopted;
- a replayed frame older than the `late_event_max_age` option updates the entities but is NOT
  fired on the bus (a late trigger is wrong), while a fresh replay and every live frame fire;
- an app that never answers the resume is dropped, and the next connect re-seeds.

Deterministic: the clock a replay's age is measured against is pinned (`_now`), the resume deadline
is shortened only for the one test that needs it to expire, and no test sleeps.
"""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.core import HomeAssistant
import pytest

from custom_components.switchboard import coordinator as coord_mod
from custom_components.switchboard.const import CONF_LATE_EVENT_MAX_AGE, EVENT_SWITCHBOARD

from .test_contract import CID
from .test_push import _frame, _LiveSession, _make, _Session

NOW = 1_800_000_000.0  # the pinned wall clock, epoch seconds


@pytest.fixture(autouse=True)
def _no_afk_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(coord_mod, "AFK_COALESCE_SECS", 0)


def _stamped(
    seq: int,
    frame: dict[str, Any],
    *,
    boot: str = "boot-1",
    replayed_secs_ago: float | None = None,
) -> Any:
    extra: dict[str, Any] = {"seq": seq, "boot_id": boot}
    if replayed_secs_ago is not None:
        extra |= {"replayed": True, "ts": int((NOW - replayed_secs_ago) * 1000)}
    return _frame({**frame, **extra})


def _scene(scene: str) -> dict[str, Any]:
    return {"type": "obs_scene_changed", "connection_id": CID, "scene": scene}


FOLLOW = {"type": "twitch_event", "kind": "follow", "connection_id": "t1"}
RESUMED = _frame({"type": "resumed", "replayed": 2})
GAP = _frame({"type": "gap"})


def _bus(hass: HomeAssistant) -> list[dict[str, Any]]:
    fired: list[dict[str, Any]] = []
    hass.bus.async_listen(EVENT_SWITCHBOARD, lambda e: fired.append(dict(e.data)))
    return fired


async def test_a_reconnect_resumes_and_the_replay_applies_once(hass: HomeAssistant) -> None:
    coord, client, _entry, waits = _make(
        hass,
        [
            _Session([_stamped(1, _scene("Live")), _stamped(2, FOLLOW)]),
            _Session(
                [
                    # Overlaps what this client already had (seq 2): dropped, not re-fired.
                    _stamped(2, FOLLOW, replayed_secs_ago=5),
                    _stamped(3, FOLLOW, replayed_secs_ago=4),
                    _stamped(4, _scene("Missed"), replayed_secs_ago=3),
                    RESUMED,
                    _stamped(5, _scene("Live again")),
                ]
            ),
        ],
    )
    coord._now = lambda: NOW
    fired = _bus(hass)
    await coord._ws_loop()
    await hass.async_block_till_done()

    assert client.resumes == [None, (2, "boot-1")], "the last seq and its boot ride the reconnect"
    assert client.reads["state"] == 1, "a resume is not a re-seed"
    assert [(f["type"], f.get("seq")) for f in fired] == [
        ("obs_scene_changed", 1),
        ("twitch_event", 2),
        ("twitch_event", 3),
        ("obs_scene_changed", 4),
        ("obs_scene_changed", 5),
    ], "every frame on the bus exactly once — seq 2 was not fired twice"
    assert coord.data.obs[CID]["current_scene"] == "Live again"
    assert coord.last_update_success is True, "`resumed` brought the entities back"
    assert [w[0] for w in waits] == [1]


async def test_entities_stay_unavailable_until_the_resume_is_confirmed(
    hass: HomeAssistant,
) -> None:
    live = _LiveSession()
    coord, _client, _entry, _waits = _make(hass, [_Session([_stamped(1, _scene("Live"))]), live])
    coord._now = lambda: NOW
    task = hass.async_create_task(coord._ws_loop())
    try:
        await live.push(_stamped(2, _scene("Missed"), replayed_secs_ago=2))
        assert coord.data.obs[CID]["current_scene"] == "Missed", "the replay patches the mirror"
        assert coord.last_update_success is False, "but it is not current until `resumed`"
        await live.push(RESUMED)
        assert coord.last_update_success is True
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_a_gap_re_seeds_and_the_new_boot_is_adopted(hass: HomeAssistant) -> None:
    coord, client, _entry, _waits = _make(
        hass,
        [
            _Session([_stamped(40, _scene("Live"))]),
            # The app restarted: it cannot resume, and its sequence starts over.
            _Session([GAP, _stamped(1, _scene("New"), boot="boot-2")]),
            _Session([RESUMED]),
        ],
    )
    await coord._ws_loop()
    await hass.async_block_till_done()

    assert client.resumes == [None, (40, "boot-1"), (1, "boot-2")]
    assert client.reads["state"] == 2, "the gap re-seeded"
    assert coord.data.obs[CID]["current_scene"] == "New", "seq 1 of a new boot is not a replay"
    assert coord.last_update_success is True


async def test_a_stale_replay_updates_the_sensors_but_is_not_fired(hass: HomeAssistant) -> None:
    """The light must not flash for a follow that happened minutes ago while the link was down —
    but the scene sensor must still show where OBS is now."""
    coord, _client, _entry, _waits = _make(
        hass,
        [
            _Session([_stamped(1, _scene("Live"))]),
            _Session(
                [
                    _stamped(2, FOLLOW, replayed_secs_ago=300),  # 5 min late: not fired
                    _stamped(3, _scene("BRB"), replayed_secs_ago=240),  # not fired, still applied
                    _stamped(4, FOLLOW, replayed_secs_ago=10),  # fresh enough: fired
                    RESUMED,
                ]
            ),
        ],
    )
    coord._now = lambda: NOW
    fired = _bus(hass)
    await coord._ws_loop()
    await hass.async_block_till_done()

    assert [f.get("seq") for f in fired] == [1, 4]
    assert coord.data.obs[CID]["current_scene"] == "BRB", "a late frame still moves the sensor"
    assert coord._last_seq == 4


async def test_the_late_limit_is_an_option(hass: HomeAssistant) -> None:
    coord, _client, entry, _waits = _make(
        hass,
        [
            _Session([_stamped(1, _scene("Live"))]),
            _Session([_stamped(2, FOLLOW, replayed_secs_ago=300), RESUMED]),
        ],
    )
    hass.config_entries.async_update_entry(entry, options={CONF_LATE_EVENT_MAX_AGE: 600})
    coord._now = lambda: NOW
    fired = _bus(hass)
    await coord._ws_loop()
    await hass.async_block_till_done()
    assert [f.get("seq") for f in fired] == [1, 2], "within a 10-minute limit it fires"


@pytest.mark.parametrize(
    ("ts", "late"),
    [
        (int((NOW - 30) * 1000), False),  # epoch ms
        (NOW - 30, False),  # epoch seconds
        ("2027-01-15T07:59:30+00:00", False),  # ISO, 30 s before NOW
        ("2027-01-12T08:00:00+00:00", True),  # ISO, 3 days before NOW
        (None, True),  # a replay whose age cannot be read is not fired blind
        ("not a time", True),
        (True, True),
    ],
)
async def test_a_replay_age_is_read_from_any_ts_spelling(
    hass: HomeAssistant, ts: Any, late: bool
) -> None:
    coord, _client, _entry, _waits = _make(hass, [_Session([])])
    coord._now = lambda: NOW
    assert coord._is_late({"type": "twitch_event", "replayed": True, "ts": ts}) is late
    assert coord._is_late({"type": "twitch_event", "ts": ts}) is False, "a live frame never is"


async def test_an_unanswered_resume_is_dropped_and_the_next_connect_re_seeds(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(coord_mod, "RESUME_TIMEOUT_SECS", 0)
    silent = _LiveSession()  # takes the resume and never says `resumed` or `gap`
    coord, client, _entry, waits = _make(
        hass,
        [_Session([_stamped(9, _scene("Live"))]), silent, _Session([_scene("Seeded")])],
    )
    await coord._ws_loop()
    await hass.async_block_till_done()

    assert client.resumes == [None, (9, "boot-1"), None], "the position was forgotten"
    assert client.reads["state"] == 2, "the connect after it re-seeded"
    assert [w[0] for w in waits] == [1, 2], "a timeout is a drop like any other: the ladder climbs"


async def test_an_older_app_without_seq_re_seeds_on_every_reconnect(hass: HomeAssistant) -> None:
    coord, client, _entry, _waits = _make(
        hass,
        [
            _Session([_frame(_scene("Live")), RESUMED]),  # a stray `resumed` changes nothing
            _Session([_frame(_scene("Back"))]),
        ],
    )
    fired = _bus(hass)
    await coord._ws_loop()
    await hass.async_block_till_done()
    assert client.resumes == [None, None]
    assert client.reads["state"] == 2
    assert [f["type"] for f in fired] == ["obs_scene_changed", "obs_scene_changed"]
