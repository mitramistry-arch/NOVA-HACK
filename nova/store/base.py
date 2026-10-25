"""Shared storage exceptions."""


class StoreError(Exception):
    """Exception raised for storage layer errors."""


class SupersessionError(StoreError):
    """Raised when a supersession link would violate chain integrity."""


class EmbeddingCompatibilityError(StoreError):
    """Raised when stored embeddings do not match the active embedder."""


class StorageUnavailableError(StoreError):
    """Raised when a storage backend cannot be reached or authenticated."""

    def __init__(self, code: str, message: str) -> None:
        """Create the error with a stable public ``code`` and a safe ``message``."""
        super().__init__(message)
        self.code = code
        self.public_message = message
