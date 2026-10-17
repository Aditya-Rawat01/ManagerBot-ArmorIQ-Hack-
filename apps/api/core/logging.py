"""Minimal logging setup. Never log secrets, tokens, raw member emails, or protected content."""

import logging
import logging.config

# Only the message is printed: `extra=` fields are dropped, so log lines put
# correlation IDs into the message itself (`... event_id=%s request_id=%s`).
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def configure_logging(level: str) -> None:
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "default": {"format": LOG_FORMAT},
            },
            "handlers": {
                "console": {"class": "logging.StreamHandler", "formatter": "default"},
            },
            "root": {"level": level, "handlers": ["console"]},
        }
    )
