from src.config import configured_retry_delay
from src.service import retry_delay


def test_configured_delay_is_two_seconds():
    assert configured_retry_delay() == 2


def test_zero_attempts_have_no_delay():
    assert retry_delay(0) == 0


def test_delay_scales_by_configured_seconds_per_attempt():
    assert retry_delay(3) == 3 * configured_retry_delay()
