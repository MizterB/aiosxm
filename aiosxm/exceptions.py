"""Exceptions raised by the aiosxm package."""


class SxmError(Exception):
    """Base class for all aiosxm errors."""


class RequestError(SxmError):
    """An error occurred while making a request."""

    def __init__(
        self,
        url: str,
        *,
        status: int | None = None,
        body: str | None = None,
        original_exception: Exception | None = None,
    ) -> None:
        """Initialize the exception."""
        detail = f"HTTP {status}" if status else str(original_exception)
        if body:
            detail = f"{detail}: {body}"
        super().__init__(f"Error for URL {url}: {detail}")
        self.url = url
        self.status = status
        self.body = body
        self.original_exception = original_exception


class AuthenticationError(SxmError):
    """An error occurred while authenticating."""

    def __init__(self, message: str, *, original_exception: Exception | None = None) -> None:
        """Initialize the exception."""
        super().__init__(message)
        self.original_exception = original_exception


class NotEntitledError(SxmError):
    """The account is authenticated but not entitled to play this content."""

    def __init__(self, message: str, *, original_exception: Exception | None = None) -> None:
        """Initialize the exception."""
        super().__init__(message)
        self.original_exception = original_exception


class SkipNotAllowedError(SxmError):
    """The account has used up its rationed skips for now.

    `more_skips_at` is the ISO-8601 time the allowance refills, when the API
    tells us; a caller can surface that rather than a bare failure.
    """

    def __init__(self, message: str, *, more_skips_at: str | None = None) -> None:
        """Initialize the exception."""
        super().__init__(message)
        self.more_skips_at = more_skips_at
