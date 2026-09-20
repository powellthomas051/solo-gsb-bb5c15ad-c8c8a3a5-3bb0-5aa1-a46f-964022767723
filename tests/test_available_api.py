"""HTTP-level tests for POST /available-occurrences."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def post(body: dict):
    return client.post("/available-occurrences", json=body)


BASE = {
    "timezone": "UTC",
    "start_local": "2025-01-01T09:00:00",
    "rrule": "FREQ=DAILY;COUNT=5",
    "duration_minutes": 60,
    "interval_start": "2025-01-01T00:00:00Z",
    "interval_end": "2025-01-10T00:00:00Z",
}


# ----------------------------------------------------------------- basics ----

def test_no_busy_returns_every_candidate_with_local_and_utc_ends():
    resp = post(BASE)
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 5
    assert body["busy_intervals"] == []
    starts = [s["start"]["utc"] for s in body["slots"]]
    assert starts == [f"2025-01-0{d}T09:00:00Z" for d in range(1, 6)]
    for day, slot in enumerate(body["slots"], start=1):
        assert slot["start"]["utc"] == f"2025-01-0{day}T09:00:00Z"
        assert slot["end"]["utc"] == f"2025-01-0{day}T10:00:00Z"
        assert slot["start"]["local"][11:] == "09:00:00"
        assert slot["end"]["local"][11:] == "10:00:00"
        assert slot["start"]["utc_offset"] == slot["end"]["utc_offset"] == "+00:00"
        assert slot["fold"] == 0
        assert slot["source"] == "rrule"


def test_slot_overlapping_busy_interval_is_filtered_but_touching_is_kept():
    # Day 2 slot is 14:00-15:00Z (New York EST): busy [13:30,14:30) intersects;
    # the [15:00,16:00) interval only touches day 3's end and must not exclude it.
    resp = post(
        {
            "timezone": "America/New_York",
            "start_local": "2025-01-01T09:00:00",
            "rrule": "FREQ=DAILY;COUNT=3",
            "duration_minutes": 60,
            "busy_intervals": [
                {"start": "2025-01-02T13:30:00Z", "end": "2025-01-02T14:30:00Z"},
                {"start": "2025-01-03T15:00:00Z", "end": "2025-01-03T16:00:00Z"},
            ],
            "interval_start": "2025-01-01T00:00:00Z",
            "interval_end": "2025-01-10T00:00:00Z",
        }
    )
    assert resp.status_code == 200
    slots = resp.json()["slots"]
    assert [s["start"]["utc"] for s in slots] == [
        "2025-01-01T14:00:00Z",
        "2025-01-03T14:00:00Z",
    ]


def test_busy_interval_at_start_endpoint_excludes_slot_but_before_start_does_not():
    resp = post(
        {
            **BASE,
            "rrule": "FREQ=DAILY;COUNT=2",
            "busy_intervals": [
                {"start": "2025-01-01T09:00:00Z", "end": "2025-01-01T09:30:00Z"},
                {"start": "2025-01-02T08:00:00Z", "end": "2025-01-02T09:00:00Z"},
            ],
        }
    )
    assert resp.status_code == 200
    # Day 1 excluded (busy covers the start); day 2 kept (busy ends exactly at start).
    assert [s["start"]["utc"] for s in resp.json()["slots"]] == ["2025-01-02T09:00:00Z"]


# --------------------------------------------------------- busy merging ------

def test_overlapping_and_adjacent_busy_intervals_are_merged():
    resp = post(
        {
            **BASE,
            "rrule": "FREQ=DAILY;COUNT=1",
            "busy_intervals": [
                {"start": "2025-01-05T00:00:00Z", "end": "2025-01-06T00:00:00Z"},
                {"start": "2025-01-01T08:00:00Z", "end": "2025-01-01T09:30:00Z"},
                {"start": "2025-01-01T09:30:00Z", "end": "2025-01-01T09:45:00Z"},  # touching
                {"start": "2025-01-01T09:40:00Z", "end": "2025-01-01T10:30:00Z"},  # overlapping
            ],
        }
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["busy_intervals"] == [
        {"start": "2025-01-01T08:00:00Z", "end": "2025-01-01T10:30:00Z"},
        {"start": "2025-01-05T00:00:00Z", "end": "2025-01-06T00:00:00Z"},
    ]
    assert body["count"] == 0


def test_busy_intervals_accept_non_utc_offsets_and_normalize_to_utc():
    resp = post(
        {
            **BASE,
            "rrule": "FREQ=DAILY;COUNT=1",
            "busy_intervals": [
                {"start": "2025-01-01T17:30:00+08:00", "end": "2025-01-01T18:30:00+08:00"},
            ],
        }
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["busy_intervals"] == [
        {"start": "2025-01-01T09:30:00Z", "end": "2025-01-01T10:30:00Z"}
    ]
    assert body["count"] == 0


# --------------------------------------------------- window constrains start --

def test_query_window_constrains_only_the_start():
    # Start 09:00 on 01-05 is inside the window [01-04, 01-05); the 90-minute
    # slot ends at 10:30Z, beyond interval_end, yet the slot must be returned.
    resp = post(
        {
            "timezone": "UTC",
            "start_local": "2025-01-04T09:00:00",
            "rrule": "FREQ=DAILY;COUNT=2",
            "duration_minutes": 90,
            "busy_intervals": [],
            "interval_start": "2025-01-04T00:00:00Z",
            "interval_end": "2025-01-05T10:00:00Z",
        }
    )
    assert resp.status_code == 200
    slots = resp.json()["slots"]
    assert [(s["start"]["utc"], s["end"]["utc"]) for s in slots] == [
        ("2025-01-04T09:00:00Z", "2025-01-04T10:30:00Z"),
        ("2025-01-05T09:00:00Z", "2025-01-05T10:30:00Z"),
    ]


# ----------------------------------------------------------- DST / folds ------

def test_repeated_wall_time_passes_are_judged_independently():
    # New York fall-back 2025-11-02: 01:30 exists at 05:30Z (fold 0, EDT) and
    # 06:30Z (fold 1, EST). A 60-minute slot:
    #   fold 0 -> [05:30, 06:30) intersects busy [05:45, 06:30) -> excluded
    #   fold 1 -> [06:30, 07:30) only touches the merged busy end -> kept
    resp = post(
        {
            "timezone": "America/New_York",
            "start_local": "2025-11-02T01:30:00",
            "duration_minutes": 60,
            "busy_intervals": [
                {"start": "2025-11-02T05:45:00Z", "end": "2025-11-02T06:15:00Z"},
                {"start": "2025-11-02T06:15:00Z", "end": "2025-11-02T06:30:00Z"},
            ],
            "interval_start": "2025-11-02T00:00:00Z",
            "interval_end": "2025-11-03T00:00:00Z",
        }
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["busy_intervals"] == [
        {"start": "2025-11-02T05:45:00Z", "end": "2025-11-02T06:30:00Z"}
    ]
    (slot,) = body["slots"]
    assert slot["fold"] == 1
    assert slot["start"] == {
        "local": "2025-11-02T01:30:00",
        "utc": "2025-11-02T06:30:00Z",
        "utc_offset": "-05:00",
        "dst": False,
        "fold": 1,
    }
    assert slot["end"]["utc"] == "2025-11-02T07:30:00Z"
    assert slot["end"]["local"] == "2025-11-02T02:30:00"


def test_both_fold_passes_returned_when_neither_is_busy():
    resp = post(
        {
            "timezone": "America/New_York",
            "start_local": "2025-11-02T01:30:00",
            "duration_minutes": 15,
            "busy_intervals": [],
            "interval_start": "2025-11-02T00:00:00Z",
            "interval_end": "2025-11-03T00:00:00Z",
        }
    )
    assert resp.status_code == 200
    slots = resp.json()["slots"]
    assert [s["fold"] for s in slots] == [0, 1]
    assert [s["start"]["utc"] for s in slots] == [
        "2025-11-02T05:30:00Z",
        "2025-11-02T06:30:00Z",
    ]
    assert [s["end"]["utc"] for s in slots] == [
        "2025-11-02T05:45:00Z",
        "2025-11-02T06:45:00Z",
    ]


def test_end_is_real_elapsed_time_across_a_dst_transition():
    # 120 real minutes from 2025-11-02 00:30 EDT (04:30Z) cross the 02:00
    # fall-back; the local clock shows 01:30 EST (three local wall hours),
    # but the UTC end is exactly 06:30Z.
    resp = post(
        {
            "timezone": "America/New_York",
            "start_local": "2025-11-02T00:30:00",
            "duration_minutes": 120,
            "busy_intervals": [],
            "interval_start": "2025-11-02T00:00:00Z",
            "interval_end": "2025-11-03T00:00:00Z",
        }
    )
    assert resp.status_code == 200
    slot = resp.json()["slots"][0]
    assert slot["start"]["utc"] == "2025-11-02T04:30:00Z"
    assert slot["end"]["utc"] == "2025-11-02T06:30:00Z"
    assert slot["start"]["local"] == "2025-11-02T00:30:00"
    assert slot["start"]["utc_offset"] == "-04:00"
    assert slot["end"]["local"] == "2025-11-02T01:30:00"
    assert slot["end"]["utc_offset"] == "-05:00"
    assert slot["end"]["dst"] is False


def test_fold_policy_later_restricts_candidates_before_busy_check():
    resp = post(
        {
            "timezone": "America/New_York",
            "start_local": "2025-11-02T01:30:00",
            "duration_minutes": 60,
            "dst_policy": {"fold": "later"},
            "busy_intervals": [
                {"start": "2025-11-02T05:30:00Z", "end": "2025-11-02T06:30:00Z"},
            ],
            "interval_start": "2025-11-02T00:00:00Z",
            "interval_end": "2025-11-03T00:00:00Z",
        }
    )
    assert resp.status_code == 200
    (slot,) = resp.json()["slots"]
    assert slot["fold"] == 1
    assert slot["start"]["utc"] == "2025-11-02T06:30:00Z"


def test_gap_skipped_instance_simply_has_no_slot():
    # 02:30 on 2025-03-09 in New York does not exist; default gap policy skip.
    resp = post(
        {
            "timezone": "America/New_York",
            "start_local": "2025-03-08T02:30:00",
            "rrule": "FREQ=DAILY;COUNT=3",
            "duration_minutes": 30,
            "busy_intervals": [],
            "interval_start": "2025-03-01T00:00:00Z",
            "interval_end": "2025-03-20T00:00:00Z",
        }
    )
    assert resp.status_code == 200
    assert [s["start"]["local"] for s in resp.json()["slots"]] == [
        "2025-03-08T02:30:00",
        "2025-03-10T02:30:00",
    ]


# ------------------------------------------------------------- rdate / exdate --

def test_rdate_source_is_preserved():
    resp = post(
        {
            "timezone": "UTC",
            "start_local": "2024-12-01T09:00:00",  # outside the window
            "rdate": ["2025-01-02T11:00:00"],
            "duration_minutes": 30,
            "interval_start": "2025-01-01T00:00:00Z",
            "interval_end": "2025-01-10T00:00:00Z",
        }
    )
    assert resp.status_code == 200
    (slot,) = resp.json()["slots"]
    assert slot["source"] == "rdate"
    assert slot["start"]["utc"] == "2025-01-02T11:00:00Z"
    assert slot["end"]["utc"] == "2025-01-02T11:30:00Z"


# ----------------------------------------------------------------- errors ----

def test_missing_duration_is_validation_error():
    body = {k: v for k, v in BASE.items() if k != "duration_minutes"}
    resp = post(body)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


def test_non_positive_duration_is_validation_error():
    resp = post({**BASE, "duration_minutes": -15})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"


def test_busy_interval_without_offset_is_invalid_datetime():
    resp = post(
        {
            **BASE,
            "rrule": "FREQ=DAILY;COUNT=1",
            "busy_intervals": [
                {"start": "2025-01-01T09:00:00", "end": "2025-01-01T10:00:00"}
            ],
        }
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_datetime"


def test_malformed_busy_interval_datetime_is_invalid_datetime():
    resp = post(
        {
            **BASE,
            "rrule": "FREQ=DAILY;COUNT=1",
            "busy_intervals": [
                {"start": "not-a-date", "end": "2025-01-01T10:00:00Z"}
            ],
        }
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_datetime"


def test_empty_or_reversed_busy_interval_is_invalid_interval():
    for interval in (
        {"start": "2025-01-01T10:00:00Z", "end": "2025-01-01T10:00:00Z"},
        {"start": "2025-01-01T11:00:00Z", "end": "2025-01-01T10:00:00Z"},
    ):
        resp = post(
            {
                **BASE,
                "rrule": "FREQ=DAILY;COUNT=1",
                "busy_intervals": [interval],
            }
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_interval"
