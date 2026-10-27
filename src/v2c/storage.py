"""Immutable artifact stores. A reference is returned only after read-back verification."""

from __future__ import annotations

import base64
import hashlib
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol


class StorageError(ValueError):
    pass


@dataclass(frozen=True)
class ArtifactRef:
    key: str
    sha256: str
    size: int

    def to_dict(self) -> dict:
        return asdict(self)


class ArtifactStore(Protocol):
    def put_bytes(self, key: str, data: bytes) -> ArtifactRef: ...
    def get_bytes(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...


def checked_key(key: str) -> str:
    if (
        not key
        or len(key) > 1024
        or "\\" in key
        or "\x00" in key
        or key.startswith("/")
        or any(p in ("", ".", "..") for p in key.split("/"))
        or ":" in key
    ):
        raise StorageError("INVALID_OBJECT_KEY")
    return PurePosixPath(key).as_posix()


def reference(key: str, data: bytes) -> ArtifactRef:
    return ArtifactRef(checked_key(key), hashlib.sha256(data).hexdigest(), len(data))


class LocalArtifactStore:
    """Development backend; atomic no-replace publication, never an overwrite."""

    def __init__(self, root: Path | str):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _path(self, key: str) -> Path:
        path = self.root / checked_key(key)
        # This root is trusted, but fail closed if another local user replaced a parent.
        if not path.resolve().is_relative_to(self.root):
            raise StorageError("INVALID_OBJECT_PATH")
        return path

    def put_bytes(self, key: str, data: bytes) -> ArtifactRef:
        ref = reference(key, data)
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(dir=target.parent, prefix=".pending-")
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(name, target)
            except FileExistsError:
                if target.read_bytes() != data:
                    raise StorageError("IMMUTABLE_OBJECT_CONFLICT") from None
            actual = self.get_bytes(key)
            if hashlib.sha256(actual).hexdigest() != ref.sha256 or len(actual) != ref.size:
                raise StorageError("OBJECT_CHECKSUM_MISMATCH")
            directory = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return ref
        finally:
            Path(name).unlink(missing_ok=True)

    def get_bytes(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()


class S3ArtifactStore:
    """Private S3 objects with conditional creation and independent checksum readback.

    Caller owns the boto3 client and its credentials. Configure bucket public-access
    blocking and TLS outside this adapter. S3-compatible endpoints must implement
    IfNoneMatch; unsupported endpoints fail closed rather than silently overwrite.
    """

    def __init__(self, client: Any, bucket: str, prefix: str = ""):
        self.client, self.bucket = client, bucket
        self.prefix = checked_key(prefix.rstrip("/")) + "/" if prefix else ""

    def _key(self, key: str) -> str:
        return self.prefix + checked_key(key)

    def put_bytes(self, key: str, data: bytes) -> ArtifactRef:
        ref = reference(key, data)
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=self._key(key),
                Body=data,
                ContentType="application/octet-stream",
                IfNoneMatch="*",
                ChecksumSHA256=base64.b64encode(bytes.fromhex(ref.sha256)).decode(),
                Metadata={"sha256": ref.sha256},
            )
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code not in {"PreconditionFailed", "412"}:
                raise StorageError("OBJECT_UPLOAD_FAILED") from None
        if self.get_bytes(key) != data:
            raise StorageError("OBJECT_CHECKSUM_MISMATCH")
        return ref

    def get_bytes(self, key: str) -> bytes:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self._key(key))
            body = response["Body"]
            try:
                data = body.read()
            finally:
                body.close()
            expected = response.get("Metadata", {}).get("sha256")
            if expected and hashlib.sha256(data).hexdigest() != expected:
                raise StorageError("OBJECT_CHECKSUM_MISMATCH")
            return data
        except StorageError:
            raise
        except Exception:
            raise StorageError("OBJECT_READ_FAILED") from None

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._key(key))
            return True
        except Exception as exc:
            code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if code in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise StorageError("OBJECT_READ_FAILED") from None
