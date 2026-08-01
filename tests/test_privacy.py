from __future__ import annotations

import logging

import pytest

from shelfsignal.cli import RedactingFilter


@pytest.mark.parametrize(
    "message,args,secrets",
    [
        (
            "Cookie: %s Authorization: Bearer %s",
            ("session=secret-value", "hidden-token"),
            ("secret-value", "hidden-token"),
        ),
        (
            "prefix cOoKiE :\n folded-secret\nAUTHORIZATION:\tBasic basic-secret",
            (),
            ("folded-secret", "basic-secret"),
        ),
    ],
)
def test_logs_redact_formatted_cookie_and_authorization(
    caplog: pytest.LogCaptureFixture,
    message: str,
    args: tuple[str, ...],
    secrets: tuple[str, ...],
) -> None:
    logger = logging.getLogger("shelfsignal.test")
    redact = RedactingFilter()
    logger.addFilter(redact)
    try:
        with caplog.at_level(logging.INFO, logger="shelfsignal.test"):
            logger.info(message, *args)
    finally:
        logger.removeFilter(redact)

    for secret in secrets:
        assert secret not in caplog.text
    assert "[REDACTED]" in caplog.text


def test_redacting_filter_does_not_mutate_unrelated_formatted_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("shelfsignal.unrelated")
    redact = RedactingFilter()
    logger.addFilter(redact)
    try:
        with caplog.at_level(logging.INFO, logger="shelfsignal.unrelated"):
            logger.info("collected %s articles", 2)
    finally:
        logger.removeFilter(redact)

    assert "collected 2 articles" in caplog.text
    assert "[REDACTED]" not in caplog.text
