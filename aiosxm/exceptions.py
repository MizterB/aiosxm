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
