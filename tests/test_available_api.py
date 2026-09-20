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
    "rrule": "FREQ=DAILY;COUNT=3",
    "interval_start": "2025-01-01T00:00:00Z",
    "interval_end": "2025-01-10T00:00:00Z",
}


# ----------------------------------------------------------------- basics ----

def test_no_busy_returns_all_slots_with_local_and_utc_ends():
    resp = post({**BASE, "duration_minutes": 60})
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 3
    assert body["busy_intervals"] == []
    assert body["duration_minutes"] == 60
    occ = body["occurrences"]
    assert [(o["utc_start"], o["utc_end"]) for o in occ] == [
        ("2025-01-01T09:00:00Z", "2025-01-01T10:00:00Z"),
        ("2025-01-02T09:00:00Z", "2025-01-02T10:00:00Z"),
        ("2025-01-03T09:00:00Z", "2025-01-03T10:00:00Z"),
    ]
    first = occ[0]
    assert first["local_start"] == "2025-01-01T09:00:00"
    assert first["local_end"] == "2025-01-01T10:00:00"
    assert first["utc_offset_start"] == "+00:00"
    assert first["utc_offset_end"] == "+00:00"
    assert first["dst_start"] is False and first["dst_end"] is False
    assert first["fold"] == 0 and first["fold_end"] == 0
    assert first["source"] == "rrule"


def test_overlapping_slot_is_dropped_and_half_open_end_is_free():
    # Busy 10:00-11:00 on day 2: day-1 slot [09:00,10:00) is free because its
    # end is exclusive; day-2 slot [09:00,10:00) is also free; make the block
    # 09:30-10:30 to drop day 2, while an exact-touching block keeps day 3.
    resp = post(
        {
            **BASE,
            "duration_minutes": 60,
            "busy_intervals": [
                {"start": "2025-01-02T09:30:00Z", "end": "2025-01-02T10:30:00Z"},
                # touches day-3 slot at exactly 10:00 -> still available
                {"start": "2025-01-03T10:00:00Z", "end": "2025-01-03T11:00:00Z"},
            ],
        }
    )
    assert resp.status_code == 200
    body = resp.json()
    assert [o["utc_start"] for o in body["occurrences"]] == [
        "2025-01-01T09:00:00Z",
        "2025-01-03T09:00:00Z",
    ]
    assert body["count"] == 2


def test_busy_block_equal_to_slot_start_blocks_and_exact_slot_end_does_not():
    # A block starting at 09:00 intersects [09:00,10:00); a block ending at
    # exactly 09:00 (half open) does adjacent, not overlapping.
    resp = post(
        {
            **BASE,
            "duration_minutes": 60,
            "busy_intervals": [
                {"start": "2025-01-01T09:00:00Z", "end": "2025-01-01T09:30:00Z"},
                {"start": "2025-01-02T08:00:00Z", "end": "2025-01-02T09:00:00Z"},
            ],
        }
    )
    assert resp.status_code == 200
    assert [o["utc_start"] for o in resp.json()["occurrences"]] == [
        "2025-01-02T09:00:00Z",
        "2025-01-03T09:00:00Z",
    ]


# --------------------------------------------------------- merge semantics ----

def test_overlapping_and_adjacent_busy_intervals_are_merged_in_response():
    resp = post(
        {
            **BASE,
            "duration_minutes": 30,
            "busy_intervals": [
                {"start": "2025-01-02T09:10:00Z", "end": "2025-01-02T09:20:00Z"},
                # overlaps the previous block
                {"start": "2025-01-02T09:15:00Z", "end": "2025-01-02T09:40:00Z"},
                # adjacent to the merged block (09:40 == previous end)
                {"start": "2025-01-02T09:40:00Z", "end": "2025-01-02T10:00:00Z"},
                # wholly contained
                {"start": "2025-01-02T09:45:00Z", "end": "2025-01-02T09:50:00Z"},
            ],
        }
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["busy_intervals"] == [
        {"start": "2025-01-02T09:10:00Z", "end": "2025-01-02T10:00:00Z"}
    ]
    # Day-2 30-minute slot [09:00,09:30) intersects the merged block; gone.
    assert "2025-01-02T09:00:00Z" not in [o["utc_start"] for o in body["occurrences"]]
    assert body["count"] == 2


def test_busy_intervals_with_offsets_are_normalized_to_utc():
    resp = post(
        {
            **BASE,
            "duration_minutes": 60,
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
    assert [o["utc_start"] for o in body["occurrences"]] == [
        "2025-01-02T09:00:00Z",
        "2025-01-03T09:00:00Z",
    ]


# ------------------------------------------------------------- real elapsed --

def test_slot_spanning_fall_back_is_real_elapsed_time():
    # New York 2025-11-02 01:30 happens twice. A 60-minute slot starting on the
    # first pass (05:30Z, EDT -04:00) ends at 06:30Z, which is 01:30 local on
    # the second pass (EST -05:00, fold_end=1).
    resp = post(
        {
            "timezone": "America/New_York",
            "start_local": "2025-11-02T01:30:00",
            "rrule": "FREQ=DAILY;COUNT=1",
            "duration_minutes": 60,
            "interval_start": "2025-11-01T00:00:00Z",
            "interval_end": "2025-11-04T00:00:00Z",
        }
    )
    assert resp.status_code == 200
    occ = resp.json()["occurrences"]
    assert [(o["utc_start"], o["utc_end"]) for o in occ] == [
        ("2025-11-02T05:30:00Z", "2025-11-02T06:30:00Z"),
        ("2025-11-02T06:30:00Z", "2025-11-02T07:30:00Z"),
    ]
    first, second = occ
    assert first["local_start"] == "2025-11-02T01:30:00"
    assert first["local_end"] == "2025-11-02T01:30:00"  # repeated wall time
    assert first["utc_offset_start"] == "-04:00"
    assert first["utc_offset_end"] == "-05:00"
    assert first["fold"] == 0 and first["fold_end"] == 1
    assert second["fold"] == 1 and second["fold_end"] == 0
    assert second["utc_offset_start"] == "-05:00"
    assert second["local_end"] == "2025-11-02T02:30:00"


def test_slot_spanning_spring_gap_keeps_real_length_in_wall_minutes():
    # 2025-03-09 New York jumps 02:00 -> 03:00. Slot at 01:30 EST lasting 90
    # real minutes ends 04:00 EDT (150 wall-clock minutes later).
    resp = post(
        {
            "timezone": "America/New_York",
            "start_local": "2025-03-09T01:30:00",
            "duration_minutes": 90,
            "interval_start": "2025-03-08T00:00:00Z",
            "interval_end": "2025-03-10T00:00:00Z",
        }
    )
    assert resp.status_code == 200
    (o,) = resp.json()["occurrences"]
    assert o["utc_start"] == "2025-03-09T06:30:00Z"
    assert o["utc_end"] == "2025-03-09T08:00:00Z"
    assert o["local_start"] == "2025-03-09T01:30:00"
    assert o["local_end"] == "2025-03-09T04:00:00"
    assert o["utc_offset_start"] == "-05:00" and o["utc_offset_end"] == "-04:00"
    assert o["dst_start"] is False and o["dst_end"] is True


def test_fractional_duration_minutes_is_supported():
    resp = post({**BASE, "duration_minutes": 1.5, "busy_intervals": []})
    assert resp.status_code == 200
    first = resp.json()["occurrences"][0]
    assert first["utc_end"] == "2025-01-01T09:01:30Z"


# -------------------------------------------------- folds judged separately --

def test_repeated_wall_time_folds_are_judged_independently():
    # Block the first pass [05:30,06:30) only; the second pass stays free.
    resp = post(
        {
            "timezone": "America/New_York",
            "start_local": "2025-11-02T01:30:00",
            "duration_minutes": 30,
            "busy_intervals": [
                {"start": "2025-11-02T05:30:00Z", "end": "2025-11-02T06:00:00Z"}
            ],
            "interval_start": "2025-11-02T00:00:00Z",
            "interval_end": "2025-11-03T00:00:00Z",
        }
    )
    assert resp.status_code == 200
    occ = resp.json()["occurrences"]
    assert [(o["utc_start"], o["fold"]) for o in occ] == [
        ("2025-11-02T06:30:00Z", 1),
    ]


def test_fold_policy_earlier_candidate_blocked_means_no_slot():
    resp = post(
        {
            "timezone": "America/New_York",
            "start_local": "2025-11-02T01:30:00",
            "dst_policy": {"fold": "earlier"},
            "duration_minutes": 30,
            "busy_intervals": [
                {"start": "2025-11-02T05:30:00Z", "end": "2025-11-02T06:30:00Z"}
            ],
            "interval_start": "2025-11-02T00:00:00Z",
            "interval_end": "2025-11-03T00:00:00Z",
        }
    )
    assert resp.status_code == 200
    assert resp.json()["count"] == 0


# ----------------------------------------------------- window bounds start ---

def test_query_window_constrains_only_the_start():
    # A 120-minute slot starting at 23:00Z ends beyond interval_end (00:00Z)
    # but is still returned because the *start* is inside [start, end).
    resp = post(
        {
            "timezone": "UTC",
            "start_local": "2025-01-01T23:00:00",
            "rrule": "FREQ=DAILY;COUNT=2",
            "duration_minutes": 120,
            "interval_start": "2025-01-01T00:00:00Z",
            "interval_end": "2025-01-02T00:00:00Z",
        }
    )
    assert resp.status_code == 200
    occ = resp.json()["occurrences"]
    assert [(o["utc_start"], o["utc_end"]) for o in occ] == [
        ("2025-01-01T23:00:00Z", "2025-01-02T01:00:00Z")
    ]
    assert occ[0]["local_end"] == "2025-01-02T01:00:00"


# ------------------------------------------------------------ rdate/source ----

def test_rdate_candidate_keeps_source_and_gap_skip_is_shared():
    # RDATE in the New York spring gap is skipped just like /occurrences.
    resp = post(
        {
            "timezone": "America/New_York",
            "start_local": "2025-03-08T12:00:00",
            "rdate": ["2025-03-09T02:30:00"],
            "duration_minutes": 30,
            "interval_start": "2025-03-08T00:00:00Z",
            "interval_end": "2025-03-10T00:00:00Z",
        }
    )
    assert resp.status_code == 200
    occ = resp.json()["occurrences"]
    assert len(occ) == 1
    assert occ[0]["source"] == "rrule"
    assert occ[0]["utc_start"] == "2025-03-08T17:00:00Z"


# ----------------------------------------------------------------- errors ----

def test_missing_duration_is_validation_error():
    resp = client.post("/available-occurrences", json=BASE)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "validation_error"
    assert any(
        d["field"].endswith("duration_minutes")
        for d in resp.json()["error"]["details"]
    )


def test_zero_and_negative_duration_are_rejected():
    for value in (0, -15):
        resp = post({**BASE, "duration_minutes": value})
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_error"


def test_busy_interval_without_offset_is_invalid_datetime():
    resp = post(
        {
            **BASE,
            "duration_minutes": 60,
            "busy_intervals": [
                {"start": "2025-01-01T09:00:00", "end": "2025-01-01T10:00:00Z"}
            ],
        }
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_datetime"
    assert "busy_intervals[0].start" in resp.json()["error"]["message"]


def test_empty_busy_interval_is_rejected():
    resp = post(
        {
            **BASE,
            "duration_minutes": 60,
            "busy_intervals": [
                {"start": "2025-01-01T10:00:00Z", "end": "2025-01-01T10:00:00Z"}
            ],
        }
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_busy_interval"


def test_reversed_busy_interval_is_rejected():
    resp = post(
        {
            **BASE,
            "duration_minutes": 60,
            "busy_intervals": [
                {"start": "2025-01-01T11:00:00Z", "end": "2025-01-01T10:00:00Z"}
            ],
        }
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_busy_interval"


def test_malformed_busy_interval_entry_is_rejected():
    resp = post(
        {
            **BASE,
            "duration_minutes": 60,
            "busy_intervals": [{"start": "2025-01-01T09:00:00Z"}],
        }
    )
    assert resp.status_code == 422  # missing 'end' caught by pydantic
    assert resp.json()["error"]["code"] == "validation_error"
