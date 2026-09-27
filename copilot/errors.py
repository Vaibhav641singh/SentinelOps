"""Domain errors, so the pipeline never imports HTTP concerns.

Only the API layer knows about status codes; everything below raises these and
lets `copilot.api` translate them.
"""


class CopilotError(RuntimeError):
    """Base for every failure this service raises deliberately."""


class UpstreamUnavailable(CopilotError):
    """A provider is overloaded or rate-limiting. Transient — retry later."""


class UpstreamRejected(CopilotError):
    """A provider refused the request outright. Retrying will not help."""


class ConfigurationError(CopilotError):
    """The service is misconfigured — a missing key, an absent collection."""
