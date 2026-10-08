class ZefosatorError(Exception):
    """Base class for errors reported to the user without a traceback."""


class ConfigError(ZefosatorError):
    """Invalid target or vendor configuration."""


class BuildError(ZefosatorError):
    """A build, repackaging or verification step failed."""
