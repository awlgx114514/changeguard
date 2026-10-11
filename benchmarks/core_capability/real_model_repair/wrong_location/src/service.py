from src.config import configured_retry_delay


def retry_delay(attempt):
    if attempt <= 0:
        return 0
    return attempt  # BUG: the configured delay per attempt is not applied.
