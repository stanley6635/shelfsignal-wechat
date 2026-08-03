from __future__ import annotations

import logging

import pytest

from shelfsignal.cli import RedactingFilter, _install_redacting_filter


@pytest.mark.parametrize(
    "message,args,secrets",
    [
        (
            "Cookie" + ": %s Authorization" + ": Bearer %s",
            ("session=secret-value", "hidden-token"),
            ("secret-value", "hidden-token"),
        ),
        (
            "prefix cOo"
            + "KiE :\n folded-secret\nAUTHORI"
            + "ZATION:\tBasic basic-secret",
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


def test_installed_redaction_covers_package_children_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    _install_redacting_filter()
    package_child = logging.getLogger("shelfsignal.child")
    unrelated = logging.getLogger("example.unrelated")
    with caplog.at_level(logging.INFO):
        package_child.info("Authorization" + ": Bearer %s", "child-secret")
        unrelated.info("unrelated value=%s", "preserved")

    assert "child-secret" not in caplog.text
    assert "Authorization" + ": [REDACTED]" in caplog.text
    assert "unrelated value=preserved" in caplog.text


def test_package_exception_chain_and_stack_are_redacted_but_useful(
    caplog: pytest.LogCaptureFixture,
) -> None:
    _install_redacting_filter()
    package_child = logging.getLogger("shelfsignal.child.exception")
    cookie_header = "Coo" + "kie:"
    auth_header = "Authori" + "zation: Bearer"
    cause_secret = "cause-" + "secret"
    exception_secret = "exception-" + "secret"
    try:
        try:
            raise ValueError(f"{cookie_header} {cause_secret}")
        except ValueError as cause:
            raise RuntimeError(f"{auth_header} {exception_secret}") from cause
    except RuntimeError:
        with caplog.at_level(logging.ERROR):
            package_child.exception("collection failed", stack_info=True)

    assert "cause-secret" not in caplog.text
    assert "exception-secret" not in caplog.text
    assert "ValueError" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "Traceback" in caplog.text
    assert "Stack" in caplog.text

    caplog.clear()
    unrelated = logging.getLogger("example.unrelated.exception")
    try:
        raise RuntimeError("Authorization" + ": Bearer unrelated-secret")
    except RuntimeError:
        with caplog.at_level(logging.ERROR):
            unrelated.exception("unrelated failed")
    assert "unrelated-secret" in caplog.text
