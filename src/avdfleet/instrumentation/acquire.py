"""Official-source acquisition for GPL-3.0 instrumentation tooling.

Magisk and LSPosed are GPL-3.0. AVD Fleet is MIT, and mixing GPL binaries into an
MIT distribution would relicense the distribution, so neither is vendored. Instead
the operator asks the running instance to fetch a **pinned** upstream release:

* every artifact has a pinned version, URL and SHA-256 measured from the official
  release asset (see ``docs/instrumentation.md``);
* the download is size-capped, HTTPS-only and verified before it is stored;
* acquisition requires explicit confirmation, because these are root toolkits;
* provenance (version, hash, source URL, timestamp) is recorded next to the file.

Acquisition is deliberately *not* installation: a downloaded module ZIP is still
imported into the library and deployed as a separate, explicit step.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..core.errors import ConflictError, NotFoundError, ValidationError
from ..core.logging import get_logger
from ..core.models import utcnow

logger = get_logger("avdfleet.instrumentation.acquire")

#: Hard ceiling for one downloaded artifact.
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class PinnedArtifact:
    """One upstream release that may be acquired."""

    key: str
    name: str
    version: str
    url: str
    filename: str
    sha256: str
    size_bytes: int
    kind: str
    min_api: int
    notes: str


@dataclass
class ArtifactStatus:
    """Presence and integrity of one pinned artifact on this host."""

    key: str
    name: str
    version: str
    kind: str
    min_api: int
    source_url: str
    pinned_sha256: str
    pinned_size_bytes: int
    present: bool = False
    verified: bool = False
    path: str = ""
    size_bytes: int = 0
    sha256: str = ""
    acquired_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


#: Pinned releases. The hashes were measured by downloading the official assets on
#: 2026-11-27; they are the integrity anchor for every acquisition.
PINNED_ARTIFACTS: tuple[PinnedArtifact, ...] = (
    PinnedArtifact(
        key="magisk",
        name="Magisk",
        version="26.1 (26100)",
        url="https://github.com/topjohnwu/Magisk/releases/download/v26.1/Magisk-v26.1.apk",
        filename="Magisk-v26.1.apk",
        sha256="ae1a02b1ab608a51d5bc9b323e0588d06d30d9987ac8da01f4710d76f705dccb",
        size_bytes=11_411_692,
        kind="apk",
        min_api=21,
        notes=(
            "Ramdisk patching is still required (rootAVD or equivalent); this APK is the"
            " payload the Magisk app installs during 'Additional Setup'."
        ),
    ),
    PinnedArtifact(
        key="lsposed",
        name="LSPosed",
        version="1.9.2 (7024)",
        url=(
            "https://github.com/LSPosed/LSPosed/releases/download/v1.9.2/"
            "LSPosed-v1.9.2-7024-zygisk-release.zip"
        ),
        filename="LSPosed-v1.9.2-7024-zygisk-release.zip",
        sha256="0ebc6bcb465d1c4b44b7220ab5f0252e6b4eb7fe43da74650476d2798bb29622",
        size_bytes=2_462_055,
        kind="module_zip",
        min_api=27,
        notes="Magisk module; needs Magisk >= 24 and Zygisk. Refuses API < 27.",
    ),
)

_BY_KEY = {artifact.key: artifact for artifact in PINNED_ARTIFACTS}


def find_artifact(key: str) -> PinnedArtifact:
    artifact = _BY_KEY.get(str(key or "").strip().lower())
    if artifact is None:
        raise NotFoundError(
            f"no pinned artifact named {key!r}",
            hint="known artifacts: " + ", ".join(sorted(_BY_KEY)),
        )
    return artifact


def _default_http_get(url: str, limit: int) -> bytes:  # pragma: no cover - network
    import httpx

    with (
        httpx.Client(follow_redirects=True, timeout=180.0) as client,
        client.stream("GET", url) as response,
    ):
        response.raise_for_status()
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > limit:
                raise ValidationError(f"download exceeded the {limit} byte limit")
            chunks.append(chunk)
    return b"".join(chunks)


class AcquisitionStore:
    """Downloads, verifies and records pinned upstream artifacts."""

    def __init__(
        self,
        root: Path,
        *,
        http_get: Callable[[str, int], bytes] | None = None,
    ) -> None:
        self._root = Path(root)
        self._http_get = http_get or _default_http_get

    @property
    def root(self) -> Path:
        return self._root

    # -- status ----------------------------------------------------------
    def artifact_path(self, artifact: PinnedArtifact) -> Path:
        return self._root / artifact.key / artifact.version.split(" ")[0] / artifact.filename

    def status(self, artifact: PinnedArtifact) -> ArtifactStatus:
        status = ArtifactStatus(
            key=artifact.key,
            name=artifact.name,
            version=artifact.version,
            kind=artifact.kind,
            min_api=artifact.min_api,
            source_url=artifact.url,
            pinned_sha256=artifact.sha256,
            pinned_size_bytes=artifact.size_bytes,
        )
        path = self.artifact_path(artifact)
        if not path.is_file():
            return status
        data_hash = _sha256_file(path)
        status.present = True
        status.path = str(path)
        status.size_bytes = path.stat().st_size
        status.sha256 = data_hash
        status.verified = data_hash == artifact.sha256
        status.acquired_at = _iso_mtime(path)
        return status

    def list_statuses(self) -> list[ArtifactStatus]:
        return [self.status(artifact) for artifact in PINNED_ARTIFACTS]

    def manifest(self) -> list[dict[str, Any]]:
        """Return the recorded provenance entries (newest first)."""
        path = self._root / "manifest.json"
        if not path.is_file():
            return []
        import json

        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        entries = document.get("artifacts") if isinstance(document, dict) else None
        if not isinstance(entries, list):
            return []
        return [entry for entry in entries if isinstance(entry, dict)]

    # -- acquisition -----------------------------------------------------
    def acquire(self, key: str, *, confirm: bool) -> ArtifactStatus:
        """Fetch and verify one pinned artifact.

        ``confirm`` must be true: these are root toolkits and the operator is
        explicitly accepting that downloading them is what they asked for.
        """
        artifact = find_artifact(key)
        if not confirm:
            raise ConflictError(
                f"acquiring {artifact.name} requires explicit confirmation",
                hint=(
                    "repeat the request with confirm=true after reviewing the pinned"
                    f" version ({artifact.version}) and its licence (GPL-3.0)"
                ),
            )

        existing = self.status(artifact)
        if existing.present and existing.verified:
            logger.info("artifact %s already present and verified", artifact.key)
            return existing
        if existing.present and not existing.verified:
            # A stale or corrupted copy is replaced rather than trusted.
            logger.warning("artifact %s is present but failed verification; replacing", artifact.key)

        limit = min(MAX_DOWNLOAD_BYTES, artifact.size_bytes + 1024 * 1024)
        logger.info("acquiring %s %s from %s", artifact.name, artifact.version, artifact.url)
        data = self._http_get(artifact.url, limit)
        if len(data) != artifact.size_bytes:
            raise ValidationError(
                f"{artifact.name} download is {len(data)} bytes, expected {artifact.size_bytes}",
                hint="the upstream release asset may have changed; do not trust this file",
            )
        digest = hashlib.sha256(data).hexdigest()
        if digest != artifact.sha256:
            raise ValidationError(
                f"{artifact.name} failed integrity verification",
                hint=(
                    f"expected sha256 {artifact.sha256}, got {digest}; the download was"
                    " discarded and nothing was installed"
                ),
            )

        target = self.artifact_path(artifact)
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, data)
        self._record(artifact, digest, len(data))
        return self.status(artifact)

    def _record(self, artifact: PinnedArtifact, digest: str, size: int) -> None:
        import json

        entries = [
            entry
            for entry in self.manifest()
            if not (entry.get("key") == artifact.key and entry.get("sha256") == digest)
        ]
        entries.insert(
            0,
            {
                "key": artifact.key,
                "name": artifact.name,
                "version": artifact.version,
                "kind": artifact.kind,
                "sha256": digest,
                "size_bytes": size,
                "source_url": artifact.url,
                "licence": "GPL-3.0",
                "acquired_at": utcnow().isoformat(),
            },
        )
        target = self._root / "manifest.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(
            target,
            json.dumps({"artifacts": entries}, indent=2, sort_keys=False).encode("utf-8"),
        )


def _atomic_write(target: Path, data: bytes) -> None:
    handle, name = tempfile.mkstemp(dir=str(target.parent), suffix=".part")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, target)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iso_mtime(path: Path) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()


__all__ = [
    "AcquisitionStore",
    "ArtifactStatus",
    "MAX_DOWNLOAD_BYTES",
    "PINNED_ARTIFACTS",
    "PinnedArtifact",
    "find_artifact",
]
