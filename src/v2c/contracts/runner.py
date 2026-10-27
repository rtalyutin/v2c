"""Successful runner replies, including the existing prepare replay shape.

The client checks reply identity before exposing these records. These types do
not reserialize replies: unknown extension keys and absent optional keys remain
exactly as received and recorded in the operation journal.
"""

from typing import Literal, NotRequired, TypedDict


class PrepareReply(TypedDict):
    workspace_id: str
    state: Literal["READY"]
    # A fresh preparation returns the archive hash; replay returns authorization.
    archive_sha256: NotRequired[str]
    generation: NotRequired[int]


class AuthorizeReply(TypedDict):
    workspace_id: str
    generation: int
    # The bundled service emits READY. Legacy clients only require identity.
    state: NotRequired[str]
