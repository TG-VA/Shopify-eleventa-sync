from datetime import UTC, datetime

import pytest

from agent import timestamps


def test_parse_datetime_from_iso_string_with_t_separator():
    dt = timestamps.parse_datetime("2026-10-04T17:26:19")
    assert dt.year == 2026
    assert dt.month == 10
    assert dt.day == 4
    assert dt.hour == 17
    assert dt.minute == 26
    assert dt.second == 19
    assert dt.tzinfo is None


def test_parse_datetime_from_iso_string_with_microseconds():
    dt = timestamps.parse_datetime("2026-10-04T17:26:19.123456")
    assert dt.microsecond == 123456
    assert dt.tzinfo is None


def test_parse_datetime_from_firebird_format():
    dt = timestamps.parse_datetime("2026-10-04 17:26:19")
    assert dt == datetime(2026, 10, 4, 17, 26, 19)
    assert dt.tzinfo is None


def test_parse_datetime_from_firebird_format_with_microseconds():
    dt = timestamps.parse_datetime("2026-10-04 17:26:19.123456")
    assert dt.microsecond == 123456
    assert dt.tzinfo is None


def test_parse_datetime_from_date_only():
    dt = timestamps.parse_datetime("2026-10-04")
    assert dt == datetime(2026, 10, 4, 0, 0, 0)
    assert dt.tzinfo is None


def test_parse_datetime_with_utc_z_suffix():
    dt = timestamps.parse_datetime("2026-10-04T17:26:19Z")
    assert dt.tzinfo is None


def test_parse_datetime_with_utc_z_suffix_microseconds():
    dt = timestamps.parse_datetime("2026-10-04T17:26:19.123Z")
    assert dt.tzinfo is None


def test_parse_datetime_with_timezone_offset():
    dt = timestamps.parse_datetime("2026-10-04T17:26:19+02:00")
    assert dt.tzinfo is None


def test_parse_datetime_with_datetime_aware():
    aware = datetime(2026, 10, 4, 17, 26, 19, tzinfo=UTC)
    dt = timestamps.parse_datetime(aware)
    assert dt.tzinfo is None


def test_parse_datetime_with_datetime_naive():
    naive = datetime(2026, 10, 4, 17, 26, 19)
    dt = timestamps.parse_datetime(naive)
    assert dt == naive
    assert dt.tzinfo is None


def test_parse_datetime_empty_string_raises():
    with pytest.raises(ValueError):
        timestamps.parse_datetime("")


def test_parse_datetime_invalid_format_raises():
    with pytest.raises(ValueError):
        timestamps.parse_datetime("not-a-date")


def test_to_firebird_datetime_returns_naive():
    result = timestamps.to_firebird_datetime("2026-10-04T17:26:19")
    assert isinstance(result, datetime)
    assert result.tzinfo is None
    assert result == datetime(2026, 10, 4, 17, 26, 19)


def test_format_firebird_timestamp_returns_correct_format():
    result = timestamps.format_firebird_timestamp("2026-10-04T17:26:19")
    assert result == "2026-10-04 17:26:19"


def test_format_firebird_timestamp_truncates_microseconds():
    result = timestamps.format_firebird_timestamp("2026-10-04T17:26:19.123456")
    assert result == "2026-10-04 17:26:19"


def test_now_firebird_timestamp_format():
    result = timestamps.now_firebird_timestamp()
    # Should match Firebird format pattern
    assert len(result) == 19
    assert result[4] == "-" and result[7] == "-"
    assert result[10] == " "
    assert result[13] == ":" and result[16] == ":"
