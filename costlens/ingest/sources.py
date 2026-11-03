"""Export sources: where CUR 2.0 deliveries are read from.

Only LocalDirectorySource is implemented and tested. S3ExportSource documents the
intended customer-owned S3 integration but is deliberately NOT implemented:
no AWS call is made anywhere in this codebase.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class SourceUnavailable(RuntimeError):
    """The source as a whole cannot be read (missing, not configured, no access)."""


class FileAccessDenied(RuntimeError):
    pass


@dataclass(frozen=True)
class DeliveryRef:
    uri: str  # stable identifier of the delivery location, e.g. a folder or S3 prefix


class ExportSource(Protocol):
    name: str

    def list_deliveries(self) -> list[DeliveryRef]:
        """All deliveries in arrival order (oldest first)."""

    def read_manifest(self, ref: DeliveryRef) -> dict:
        """Parsed manifest JSON. Raises FileAccessDenied or FileNotFoundError."""

    def read_data_file(self, ref: DeliveryRef, key: str) -> bytes | None:
        """File bytes, or None if the file is missing. Raises FileAccessDenied."""


class LocalDirectorySource:
    """Reads deliveries from a local folder.

    Layout: <root>/<delivery-folder>/manifest.json plus the data files it lists.
    Each delivery folder stands for one export execution landing in S3. Folders
    are processed in sorted name order to simulate arrival order.
    """

    name = "local-directory"

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def list_deliveries(self) -> list[DeliveryRef]:
        if not self.root.exists():
            raise SourceUnavailable(f"export folder not found: {self.root}")
        try:
            folders = sorted(p for p in self.root.iterdir() if p.is_dir() and (p / "manifest.json").exists())
        except PermissionError as exc:
            raise SourceUnavailable(f"permission denied listing {self.root}") from exc
        return [DeliveryRef(uri=str(p)) for p in folders]

    def _safe_path(self, ref: DeliveryRef, key: str) -> Path:
        base = Path(ref.uri).resolve()
        target = (base / key).resolve()
        # Manifest content is untrusted: refuse keys that escape the delivery folder.
        if base != target and base not in target.parents:
            raise FileAccessDenied(f"data file key escapes delivery folder: {key!r}")
        return target

    def read_manifest(self, ref: DeliveryRef) -> dict:
        path = Path(ref.uri) / "manifest.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except PermissionError as exc:
            raise FileAccessDenied(f"permission denied reading {path.name}") from exc

    def read_data_file(self, ref: DeliveryRef, key: str) -> bytes | None:
        path = self._safe_path(ref, key)
        if not path.exists():
            return None
        try:
            return path.read_bytes()
        except PermissionError as exc:
            raise FileAccessDenied(f"permission denied reading {key}") from exc


class S3ExportSource:
    """PLANNED, NOT IMPLEMENTED. Customer-owned S3 bucket holding Data Exports.

    Intended design (see docs/ROADMAP.md):
      * Runs in the customer's AWS account, assuming a read-only role.
      * Needs only s3:ListBucket on the export prefix and s3:GetObject on
        <prefix>/<export-name>/*, plus kms:Decrypt if the bucket uses SSE-KMS.
      * Maps the real AWS Data Exports manifest to the DeliveryManifest contract.
    """

    name = "s3"

    def __init__(self, *_args, **_kwargs):
        raise NotImplementedError(
            "S3 ingestion is not implemented in this release. No AWS access is attempted."
        )
