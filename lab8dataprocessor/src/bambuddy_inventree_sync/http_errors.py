class ExternalApiError(RuntimeError):
    pass


class BuildOrderError(RuntimeError):
    """A user-correctable Build Order validation or workflow error."""

    pass
