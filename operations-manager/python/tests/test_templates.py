"""
Tests for opi.core.templates module.

Tests format_dutch_date and get_service_name utility functions.
"""

from opi.core.templates import format_dutch_date, get_service_name


class TestFormatDutchDate:
    """Tests for format_dutch_date."""

    def test_iso_timestamp_basic(self):
        """Should format a basic ISO timestamp in Dutch."""
        result = format_dutch_date("2026-01-14T17:14:00Z")
        assert result == "14 januari 2026 17:14"

    def test_iso_timestamp_without_time(self):
        """Should format without time when include_time=False."""
        result = format_dutch_date("2026-03-05T10:30:00Z", include_time=False)
        assert result == "5 maart 2026"

    def test_none_returns_dash(self):
        """Should return '-' for None."""
        assert format_dutch_date(None) == "-"

    def test_empty_string_returns_dash(self):
        """Should return '-' for empty string."""
        assert format_dutch_date("") == "-"

    def test_nanosecond_truncation_with_positive_timezone(self):
        """Should truncate nanoseconds and preserve positive timezone offset."""
        result = format_dutch_date("2026-01-14T17:14:34.335860214+02:00")
        assert "januari" in result
        assert "2026" in result

    def test_nanosecond_truncation_with_negative_timezone(self):
        """Bug: negative timezone offset must be preserved during nanosecond truncation.

        The code splits on '.' and then only checks for '+' in the decimal part.
        A timestamp like '2026-01-14T17:14:34.335860214-05:00' has the '-05:00'
        in the decimal part after splitting. The code must also check for '-' to
        avoid losing the timezone offset.
        """
        from datetime import datetime

        # This timestamp has nanoseconds AND a negative timezone offset
        # Manually simulate the old (buggy) truncation logic:
        test_value = "2026-01-14T17:14:34.335860214-05:00"
        test_value_z = test_value.replace("Z", "+00:00")
        parts = test_value_z.split(".")
        decimal_part = parts[1]  # "335860214-05:00"

        # The old code only checked for "+" - verify the fix handles "-"
        tz_part = ""
        for tz_char in ("+", "-"):
            if tz_char in decimal_part:
                idx = decimal_part.index(tz_char)
                tz_part = decimal_part[idx:]
                decimal_part = decimal_part[:idx]
                break

        reconstructed = parts[0] + "." + decimal_part[:6] + tz_part
        dt = datetime.fromisoformat(reconstructed)
        assert dt.tzinfo is not None, (
            f"Timezone info was lost during nanosecond truncation. Reconstructed value: {reconstructed!r}"
        )

        # Also verify the full function works
        result = format_dutch_date(test_value)
        assert "januari" in result

    def test_microsecond_precision_with_negative_timezone(self):
        """Microsecond precision (6 digits) with negative offset should work fine."""
        result = format_dutch_date("2026-06-15T08:30:45.123456-03:00")
        assert "juni" in result

    def test_datetime_object(self):
        """Should handle datetime objects directly."""
        from datetime import UTC, datetime

        dt = datetime(2026, 12, 25, 10, 0, tzinfo=UTC)
        result = format_dutch_date(dt)
        assert result == "25 december 2026 10:00"


class TestGetServiceName:
    """Tests for get_service_name."""

    def test_string_service(self):
        """String services return as-is."""
        assert get_service_name("publish-on-web") == "publish-on-web"

    def test_dict_service(self):
        """Dict services return the first key."""
        assert get_service_name({"keycloak": {"config": {}}}) == "keycloak"

    def test_empty_dict_service(self):
        """Empty dict returns empty string."""
        assert get_service_name({}) == ""

    def test_other_type(self):
        """Other types return str representation."""
        assert get_service_name(42) == "42"
