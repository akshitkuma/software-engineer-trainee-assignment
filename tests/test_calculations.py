
from datetime import datetime

from app.main import compute_late_minutes, compute_overtime


def test_late_minutes_grace_period():
    cases = [
        ("09:35:00", 0),
        ("09:40:00", 0),
        ("09:40:01", 10),
        ("10:15:59", 45),
    ]

    for time_str, expected in cases:
        punch = datetime.strptime(
            f"2026-10-12 {time_str}",
            "%Y-%m-%d %H:%M:%S",
        )
        assert compute_late_minutes(punch, "09:30") == expected


def test_overtime_threshold():
    cases = [
        ("18:59:00", 0),
        ("19:00:00", 30),
        ("19:15:00", 45),
    ]

    for time_str, expected in cases:
        punch = datetime.strptime(
            f"2026-10-12 {time_str}",
            "%Y-%m-%d %H:%M:%S",
        )
        assert (
            compute_overtime(punch, "18:30", "2026-10-12")
            == expected
        )

def test_overnight_shift_late_minutes():
    # Punch-in at 22:05 for a 22:00 shift: not late.
    punch = datetime.strptime(
        "2026-10-12 22:05:00",
        "%Y-%m-%d %H:%M:%S",
    )
    assert compute_late_minutes(punch, "22:00") == 0

def test_overnight_shift_overtime():
    # Shift: 22:00 to 06:00, attendance date 2026-10-12.
    # Punch-out at 06:30 next day should count 30 overtime minutes.
    punch = datetime.strptime(
        "2026-10-13 06:30:00", "%Y-%m-%d %H:%M:%S"
    )
    assert compute_overtime(punch, "06:00", "2026-10-12") == 30
