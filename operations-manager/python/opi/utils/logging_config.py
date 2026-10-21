import logging
import logging.handlers
from pathlib import Path
from typing import ClassVar

from opi.core.flow_id import get_flow_id
from opi.utils.logging_redact import SecretRedactionFilter, redact_invite_key_in_path

# Replace the default LogRecord factory so that every record — regardless of
# which logger, handler, or third-party library creates it — carries flow_id.
_original_factory = logging.getLogRecordFactory()


def _flow_id_record_factory(*args: object, **kwargs: object) -> logging.LogRecord:
    record = _original_factory(*args, **kwargs)
    record.flow_id = get_flow_id()  # type: ignore[attr-defined]
    return record


logging.setLogRecordFactory(_flow_id_record_factory)


class HealthEndpointFilter(logging.Filter):
    """Filter to exclude health and infrastructure endpoint requests from uvicorn access logs."""

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
    """Houdt de uitnodigingssleutel uit de access-log van uvicorn.

    De sleutel staat in het PAD (``/invite/<sleutel>``), en de access-regel schrijft dat pad
    letterlijk, dus zonder dit filter zet elk verzoek in de uitnodigingsflow het geheim in
    Loki. Het pad is een van de ``args`` van de regel, dus die worden omgezet en niet de al
    opgemaakte tekst.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(redact_invite_key_in_path(arg) if isinstance(arg, str) else arg for arg in record.args)
        return True


#: Een instantie, want dit filter SCHRIJFT in de regel en ``setup_logging`` wordt twee keer
#: aangeroepen (early_logging en config). ``addFilter`` slaat een filter over dat er al staat,
#: dus een nieuwe instantie per aanroep zou het pad twee keer omzetten en de vingerafdruk in
#: de access-log een andere maken dan die in de regels van de routes.
_INVITE_KEY_PATH_FILTER = InviteKeyPathFilter()


def setup_logging(
    log_to_file: bool = False,
    log_file_path: str = "log.txt",
    log_errors_to_file: bool = False,
    log_errors_file_path: str = "/data/logs/errors.log",
    level: str = "DEBUG",
) -> None:
    """
    Configure logging to output to stdout and optionally to file(s).

    Args:
        log_to_file: Whether to enable file logging alongside stdout
        log_file_path: Path to log file when file logging is enabled
        log_errors_to_file: Whether to write WARNING+ to a separate error log file
        log_errors_file_path: Path to error log file (should be on persistent storage)
        level: Level name for the ``opi`` logger; an unknown name falls back to INFO
    """
    # Create logger configuration
    log_format = "%(asctime)s - %(name)s - %(levelname)s - [%(flow_id)s] %(message)s"

    # Clear any existing handlers
    root_logger = logging.getLogger()
    root_logger.handlers.clear()

    # Set global log level to INFO to reduce noise from external packages
    root_logger.setLevel(logging.INFO)

    # Het niveau van de opi-logger bepaalt wat er uit de applicatie komt; de handlers staan
    # op DEBUG zodat deze instelling de enige grens is.
    opi_level, level_warning = _resolve_level(level)
    opi_logger = logging.getLogger("opi")
    opi_logger.setLevel(opi_level)

    # Tweede verdedigingslaag naast redact_sensitive_headers: maskeert een geheim dat toch
    # in een bericht belandt, ook in dat van een bibliotheek.
    redaction_filter = SecretRedactionFilter()

    # Filter out health endpoint requests from uvicorn access logs
    uvicorn_access_logger = logging.getLogger("uvicorn.access")
    uvicorn_access_logger.addFilter(HealthEndpointFilter())
    uvicorn_access_logger.addFilter(_INVITE_KEY_PATH_FILTER)

    # Always add stdout handler
    stdout_handler = logging.StreamHandler()
    stdout_handler.setLevel(logging.DEBUG)
    stdout_handler.setFormatter(logging.Formatter(log_format))
    stdout_handler.addFilter(redaction_filter)
    root_logger.addHandler(stdout_handler)

    if level_warning:
        logging.warning(level_warning)  # noqa: LOG015

    # Add file handler if requested
    if log_to_file:
        try:
            # Ensure log directory exists
            log_path = Path(log_file_path)
            log_path.parent.mkdir(parents=True, exist_ok=True)

            # Create rotating file handler (max 10MB, keep 5 files)
            file_handler = logging.handlers.RotatingFileHandler(
                log_file_path,
                maxBytes=10 * 1024 * 1024,  # 10MB
                backupCount=5,
            )
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(logging.Formatter(log_format))
            file_handler.addFilter(redaction_filter)
            root_logger.addHandler(file_handler)

            logging.info(f"File logging enabled: {log_file_path}")  # noqa: LOG015

        except Exception as e:
            logging.exception(f"Failed to setup file logging to {log_file_path}: {e}")  # noqa: LOG015
            logging.info("Continuing with stdout logging only")  # noqa: LOG015
    else:
        logging.info("File logging disabled - using stdout only")  # noqa: LOG015

    # Add error-only file handler for persistent failure monitoring
    if log_errors_to_file:
        try:
            error_log_path = Path(log_errors_file_path)
            error_log_path.parent.mkdir(parents=True, exist_ok=True)

            # Rotating file handler: 100MB max, keep 3 backups (up to 400MB total)
            error_file_handler = logging.handlers.RotatingFileHandler(
                log_errors_file_path,
                maxBytes=100 * 1024 * 1024,  # 100MB
                backupCount=3,
            )
            error_file_handler.setLevel(logging.WARNING)
            error_file_handler.setFormatter(logging.Formatter(log_format))
            error_file_handler.addFilter(redaction_filter)
            root_logger.addHandler(error_file_handler)

            logging.info(f"Error file logging enabled: {log_errors_file_path}")  # noqa: LOG015

        except Exception as e:
            logging.exception(f"Failed to setup error file logging to {log_errors_file_path}: {e}")  # noqa: LOG015
            logging.info("Continuing without error file logging")  # noqa: LOG015
