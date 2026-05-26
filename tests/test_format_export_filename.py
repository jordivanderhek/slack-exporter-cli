from datetime import datetime, timezone

from slack_export import _format_export_filename


def _dt(
    y: int, m: int, d: int, *, hour: int = 0, minute: int = 0, second: int = 0
) -> datetime:
    return datetime(y, m, d, hour, minute, second, tzinfo=timezone.utc)


def test_single_day_collapses_to_one_date():
    name = _format_export_filename(
        "C0AG5NW58NN",
        _dt(2026, 5, 12),
        _dt(2026, 5, 12, hour=23, minute=59, second=59),
    )
    assert name == "C0AG5NW58NN_260512.txt"


def test_multi_day_uses_two_dates():
    name = _format_export_filename(
        "D0ACJNS7FC5",
        _dt(2026, 4, 4),
        _dt(2026, 5, 4, hour=23, minute=59, second=59),
    )
    assert name == "D0ACJNS7FC5_260404_260504.txt"


def test_year_boundary_sorts_lexicographically():
    name = _format_export_filename(
        "C0AG5NW58NN",
        _dt(2025, 12, 31),
        _dt(2026, 1, 1, hour=23, minute=59, second=59),
    )
    assert name == "C0AG5NW58NN_251231_260101.txt"
    assert "251231" < "260101"
