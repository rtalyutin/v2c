"""Stable workflow error codes shared by control-plane components."""


class DomainError(Exception):
    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)
