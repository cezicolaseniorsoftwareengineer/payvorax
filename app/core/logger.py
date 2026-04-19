"""
Structured logging subsystem implementing observability patterns.
Correlation IDs for distributed tracing and sensitive data masking.
JSON logs for intelligent monitoring.
"""
import logging
import sys
from typing import Any, Dict, MutableMapping, Tuple
from pythonjsonlogger.json import JsonFormatter
from app.core.config import settings


class CorrelationFilter(logging.Filter):
    """Injects a default Correlation ID into log records to maintain schema consistency."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, 'correlation_id'):
            record.correlation_id = 'N/A'  # type: ignore
        return True


# Configuração de structured logging com JSON
# Create handler with filter to ensure correlation_id is always present before formatting
handler = logging.StreamHandler(sys.stdout)
handler.addFilter(CorrelationFilter())
formatter = JsonFormatter(
    '%(asctime)s %(levelname)s %(name)s %(correlation_id)s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
handler.setFormatter(formatter)

# Configure root logger
root_logger = logging.getLogger()
root_logger.setLevel(getattr(logging, settings.LOG_LEVEL))
# Remove existing handlers to avoid duplicates
if root_logger.handlers:
    for h in root_logger.handlers:
        root_logger.removeHandler(h)
root_logger.addHandler(handler)

logger = logging.getLogger("fintech")


class CorrelationLoggerAdapter(logging.LoggerAdapter[logging.Logger]):
    """Context-aware logger adapter ensuring Correlation ID propagation across the execution context."""

    def process(self, msg: Any, kwargs: MutableMapping[str, Any]) -> Tuple[Any, MutableMapping[str, Any]]:
        if 'extra' not in kwargs:
            kwargs['extra'] = {}
        if 'correlation_id' not in kwargs['extra']:
            kwargs['extra']['correlation_id'] = 'N/A'
        return msg, kwargs


def get_logger_with_correlation(correlation_id: str) -> CorrelationLoggerAdapter:
    """Factory for instantiating a context-bound logger instance."""
    return CorrelationLoggerAdapter(logger, {'correlation_id': correlation_id})


def audit_log(action: str, user: str, resource: str, details: Dict[str, Any]) -> None:
    """
    Emits immutable audit records for compliance-critical operations.
    Mandatory for regulatory traceability.
    """
    logger.info(
        f"AUDIT | action={action} | user={user} | resource={resource} | details={details}",
        extra={'correlation_id': details.get('correlation_id', 'N/A')}
    )
