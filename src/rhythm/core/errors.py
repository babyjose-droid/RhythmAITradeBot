"""Exception hierarchy."""


class RhythmError(Exception):
    """Base class for all application errors."""


class ConfigurationError(RhythmError):
    """Invalid or unsafe configuration."""


class LiveTradingDisabledError(RhythmError):
    """Raised by any code path that would require live trading.

    Live trading is not implemented before the Phase 10 activation gate.
    """


class AuditIntegrityError(RhythmError):
    """The audit hash chain failed verification."""
