"""Platform log format: UTC timestamp, level, logger, thread, file:line function() for troubleshooting.

configure() runs when app.main is imported. uvicorn sets up its own logging before it imports the app, so this
replaces uvicorn's formats too (access lines included); no `--log-config` flag is needed. LOG_LEVEL (default INFO).

    2026-10-29T14:03:12.345Z INFO    app.reconciler [reconcile] reconciler.py:123 reconcile_app() message
"""
import logging
import logging.config
import os
import time

FORMAT = "%(asctime)s %(levelname)-7s %(name)s [%(threadName)s] %(filename)s:%(lineno)d %(funcName)s() %(message)s"
# The call site of an access line is always uvicorn's h11/httptools module, so it carries no file:line.
ACCESS_FORMAT = '%(asctime)s %(levelname)-7s access %(client_addr)s "%(request_line)s" %(status_code)s'


class UTCFormatter(logging.Formatter):
    converter = time.gmtime
    default_time_format = "%Y-%m-%dT%H:%M:%S"
    default_msec_format = "%s.%03dZ"


class UTCAccessFormatter(UTCFormatter):
    """uvicorn passes (client_addr, method, path, http_version, status_code) as record.args."""

    def formatMessage(self, record: logging.LogRecord) -> str:
        if isinstance(record.args, tuple) and len(record.args) == 5:
            client_addr, method, path, version, status = record.args
            record.__dict__.update(client_addr=client_addr, request_line=f"{method} {path} HTTP/{version}",
                                   status_code=status)
        else:  # not uvicorn's shape: fall back to the plain message
            record.__dict__.update(client_addr="-", request_line=record.getMessage(), status_code="-")
        return super().formatMessage(record)


def config_dict(level: str | None = None) -> dict:
    level = (level or os.environ.get("LOG_LEVEL") or "INFO").upper()
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {"()": UTCFormatter, "fmt": FORMAT},
            "access": {"()": UTCAccessFormatter, "fmt": ACCESS_FORMAT},
        },
        "handlers": {
            "default": {"class": "logging.StreamHandler", "formatter": "default", "stream": "ext://sys.stderr"},
            "access": {"class": "logging.StreamHandler", "formatter": "access", "stream": "ext://sys.stdout"},
        },
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": level, "propagate": False},
            "uvicorn.error": {"handlers": ["default"], "level": level, "propagate": False},
            "uvicorn.access": {"handlers": ["access"], "level": level, "propagate": False},
            # Kept quiet: apscheduler logs every 15 s reconcile run; httpx logs every request URL (presigned
            # query strings included).
            "apscheduler": {"level": "WARNING"},
            "httpx": {"level": "WARNING"},
            "httpcore": {"level": "WARNING"},
        },
        "root": {"handlers": ["default"], "level": level},
    }


def configure(level: str | None = None) -> None:
    logging.config.dictConfig(config_dict(level))
