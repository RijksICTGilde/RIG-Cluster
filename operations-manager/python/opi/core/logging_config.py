import logging
import logging.handlers
from pathlib import Path
from typing import ClassVar

from opi.core.flow_id import get_flow_id
from opi.core.logging_redact import SecretRedactionFilter, redact_invite_key_in_path

# Replace the default LogRecord factory so that every record carries flow_id, whichever
# logger, handler or third-party library created it.
_original_factory = logging.getLogRecordFactory()


def _flow_id_record_factory(*args: object, **kwargs: object) -> logging.LogRecord:
    record = _original_factory(*args, **kwargs)
    record.flow_id = get_flow_id()  # type: ignore[attr-defined]
    return record


logging.setLogRecordFactory(_flow_id_record_factory)


class HealthEndpointFilter(logging.Filter):
    """Keeps the health and infrastructure endpoints out of the uvicorn access log."""

    EXCLUDED_PATHS: ClassVar[list[str]] = ["/health", "/readyz", "/metrics"]

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not any(f'"GET {path}' in message or f'"HEAD {path}' in message for path in self.EXCLUDED_PATHS)


def _resolve_level(level: str) -> tuple[int, str | None]:
    """Return the numeric level for ``level``, plus a warning when the name is unusable."""
    resolved = logging.getLevelNamesMapping().get(level.strip().upper())
    if resolved is None:
        return logging.INFO, f"Unknown LOG_LEVEL '{level}', falling back to INFO"
    return resolved, None


class InviteKeyPathFilter(logging.Filter):
    """Keeps the invite key out of the uvicorn access log.

    The key sits in the PATH (``/invite/<key>``), and the access line writes that path
    verbatim, so without this filter every request in the invite flow puts the secret in
    Loki. The path is one of the record's ``args``, so those are rewritten and not the
    already formatted text.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(redact_invite_key_in_path(arg) if isinstance(arg, str) else arg for arg in record.args)
        return True


#: One instance: this filter WRITES into the record, ``setup_logging`` runs twice
#: (early_logging and config), and ``addFilter`` skips a filter that is already there. A
#: fresh instance per call rewrites the path twice.
_INVITE_KEY_PATH_FILTER = InviteKeyPathFilter()


def setup_logging(
    log_to_file: bool = False,
    log_file_path: str = "log.txt",
    log_errors_to_file: bool = False,
    log_errors_file_path: str = "/data/logs/errors.log",
    level: str = "DEBUG",
) -> None:
    """Send logging to stdout, and optionally to a log file and a WARNING+ error file.

    ``log_errors_file_path`` belongs on persistent storage, since that file is what failure
    monitoring reads back. ``level`` names the level of the ``opi`` logger; an unknown name
    falls back to INFO.
    """
    log_format = "%(asctime)s - %(name)s - %(levelname)s - [%(flow_id)s] %(message)s"

    root_logger = logging.getLogger()
    root_logger.handlers.clear()

    # INFO on the root logger, to keep the noise of external packages down.
    root_logger.setLevel(logging.INFO)

    # The level of the opi logger decides what comes out of the application; the handlers sit
    # on DEBUG so that this setting is the only boundary.
    opi_level, level_warning = _resolve_level(level)
    opi_logger = logging.getLogger("opi")
    opi_logger.setLevel(opi_level)

    # A second line of defence next to redact_sensitive_headers: masks a secret that ends up
    # in a message anyway, in a library's message too.
    redaction_filter = SecretRedactionFilter()

    uvicorn_access_logger = logging.getLogger("uvicorn.access")
    uvicorn_access_logger.addFilter(HealthEndpointFilter())
    uvicorn_access_logger.addFilter(_INVITE_KEY_PATH_FILTER)

    stdout_handler = logging.StreamHandler()
    stdout_handler.setLevel(logging.DEBUG)
    stdout_handler.setFormatter(logging.Formatter(log_format))
    stdout_handler.addFilter(redaction_filter)
    root_logger.addHandler(stdout_handler)

    if level_warning:
        logging.warning(level_warning)  # noqa: LOG015  # this function configures the root logger itself

    if log_to_file:
        try:
            log_path = Path(log_file_path)
            log_path.parent.mkdir(parents=True, exist_ok=True)

            file_handler = logging.handlers.RotatingFileHandler(
                log_file_path,
                maxBytes=10 * 1024 * 1024,
                backupCount=5,
            )
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(logging.Formatter(log_format))
            file_handler.addFilter(redaction_filter)
            root_logger.addHandler(file_handler)

            logging.info(f"File logging enabled: {log_file_path}")  # noqa: LOG015  # this function configures the root logger itself

        except Exception as e:
            logging.exception(f"Failed to setup file logging to {log_file_path}: {e}")  # noqa: LOG015  # this function configures the root logger itself
            logging.info("Continuing with stdout logging only")  # noqa: LOG015  # this function configures the root logger itself
    else:
        logging.info("File logging disabled - using stdout only")  # noqa: LOG015  # this function configures the root logger itself

    # A WARNING+ file next to stdout, so a failure stays readable after the pod is gone.
    if log_errors_to_file:
        try:
            error_log_path = Path(log_errors_file_path)
            error_log_path.parent.mkdir(parents=True, exist_ok=True)

            # 100MB plus 3 backups, so at most 400MB on that volume.
            error_file_handler = logging.handlers.RotatingFileHandler(
                log_errors_file_path,
                maxBytes=100 * 1024 * 1024,
                backupCount=3,
            )
            error_file_handler.setLevel(logging.WARNING)
            error_file_handler.setFormatter(logging.Formatter(log_format))
            error_file_handler.addFilter(redaction_filter)
            root_logger.addHandler(error_file_handler)

            logging.info(f"Error file logging enabled: {log_errors_file_path}")  # noqa: LOG015  # this function configures the root logger itself

        except Exception as e:
            logging.exception(f"Failed to setup error file logging to {log_errors_file_path}: {e}")  # noqa: LOG015  # this function configures the root logger itself
            logging.info("Continuing without error file logging")  # noqa: LOG015  # this function configures the root logger itself
