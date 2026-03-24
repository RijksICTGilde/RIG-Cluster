import logging
import logging.handlers
from pathlib import Path
from typing import ClassVar

from opi.core.flow_id import get_flow_id

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


def setup_logging(
    log_to_file: bool = False,
    log_file_path: str = "log.txt",
    log_errors_to_file: bool = False,
    log_errors_file_path: str = "/data/logs/errors.log",
) -> None:
    """
    Configure logging to output to stdout and optionally to file(s).

    Args:
        log_to_file: Whether to enable file logging alongside stdout
        log_file_path: Path to log file when file logging is enabled
        log_errors_to_file: Whether to write WARNING+ to a separate error log file
        log_errors_file_path: Path to error log file (should be on persistent storage)
    """
    # Create logger configuration
    log_format = "%(asctime)s - %(name)s - %(levelname)s - [%(flow_id)s] %(message)s"

    # Clear any existing handlers
    root_logger = logging.getLogger()
    root_logger.handlers.clear()

    # Set global log level to INFO to reduce noise from external packages
    root_logger.setLevel(logging.INFO)

    # Set all OPI modules to DEBUG level for detailed application logging
    opi_logger = logging.getLogger("opi")
    opi_logger.setLevel(logging.DEBUG)

    # Configure specific module log levels for known noisy modules
    logging.getLogger("jinja_roos_components.extension").setLevel(logging.INFO)

    # Filter out health endpoint requests from uvicorn access logs
    uvicorn_access_logger = logging.getLogger("uvicorn.access")
    uvicorn_access_logger.addFilter(HealthEndpointFilter())

    # Always add stdout handler
    stdout_handler = logging.StreamHandler()
    stdout_handler.setLevel(logging.DEBUG)
    stdout_handler.setFormatter(logging.Formatter(log_format))
    root_logger.addHandler(stdout_handler)

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
            root_logger.addHandler(error_file_handler)

            logging.info(f"Error file logging enabled: {log_errors_file_path}")  # noqa: LOG015

        except Exception as e:
            logging.exception(f"Failed to setup error file logging to {log_errors_file_path}: {e}")  # noqa: LOG015
            logging.info("Continuing without error file logging")  # noqa: LOG015
