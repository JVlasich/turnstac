"""Project-wide logging.

Entrypoints call setup() once;
every module takes its own logger via logging.getLogger(__name__)
OPALS modules derive their log levels from the root logger via opals_log().
"""

import logging
import os
import sys
import tempfile

_ORANGE = "\033[38;5;208m"
_RESET = "\033[0m"

LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "none": logging.CRITICAL + 10,
}


class _ColorFormatter(logging.Formatter):
    """Colors warning lines orange, everything else untouched."""

    def format(self, record):
        s = super().format(record)
        if record.levelno == logging.WARNING:
            return f"{_ORANGE}{s}{_RESET}"
        return s


def setup(level: str = "info") -> None:
    """Configure the root logger for console output (stderr)."""
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        _ColorFormatter(
            "%(asctime)s %(levelname)-8s %(name)s | %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    logging.basicConfig(
        level=LEVELS[level],
        handlers=[handler],
    )


def opals_log(mod) -> None:
    """Project logging policy onto an OPALS module or workflow.

    Screen level from the root logger: debug -> opals info, none -> opals none,
    everything else -> opals error.
    """
    import opals

    root = logging.getLogger().getEffectiveLevel()
    if root <= logging.DEBUG:
        screen = opals.Types.LogLevel.info
    elif root > logging.CRITICAL:
        screen = opals.Types.LogLevel.none
    else:
        screen = opals.Types.LogLevel.error

    target = mod.commons if hasattr(mod, "commons") else mod
    target.screenLogLevel = screen
    target.fileLogLevel = opals.Types.LogLevel.none
    if hasattr(target, "logFile"):
        target.logFile = os.path.join(tempfile.gettempdir(), "opalsLog.xml")
