class BotError(Exception):
    """Base error shown to the user without a traceback by the CLI."""


class ConfigError(BotError):
    """Configuration is missing or unsafe."""


class MarketResolutionError(BotError):
    """A market/outcome could not be resolved unambiguously."""


class ApiError(BotError):
    """A remote public API request failed."""
