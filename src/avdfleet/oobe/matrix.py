"""OOBE matrix access with evidence enforcement.

The checked-in ``oobe_matrix.json`` is owned by the image catalogue
(:class:`avdfleet.core.interfaces.ImageCatalog`).  This module wraps it so the
rest of the OOBE service (and the API/CLI) never claims an image is verified
without the two pieces of evidence required by BRIEF §3.5:

* a top activity naming the Setup Wizard, and
* a saved screenshot.

Records that claim ``verified`` without both are downgraded to ``unverified``
(with a note and a warning) when the matrix is read.  Real records written by
:meth:`OobeMatrixRegistry.upsert` always carry their evidence paths.
"""

from __future__ import annotations

import logging
from typing import Any

from ..core.errors import OobeError
from ..core.logging import get_logger, redact
from ..core.models import OobeMatrix, OobeRecord, OobeStatus, utcnow

GOLDEN_SNAPSHOT = "avdfleet-golden"
"""Name of the golden OOBE snapshot saved by ``ensure_golden_snapshot``."""


class OobeMatrixRegistry:
    """Read/write access to an injected image catalogue's OOBE matrix."""

    def __init__(self, catalog: Any, *, logger_: logging.Logger | None = None) -> None:
        if catalog is None:
            raise OobeError(
                "an ImageCatalog is required to store the OOBE matrix",
                hint="wire the image service into the OOBE manager",
            )
        self._catalog = catalog
        self._logger = logger_ or get_logger("avdfleet.oobe.matrix")
        self._warnings: list[str] = []

    # -- reads -----------------------------------------------------------
    @property
    def warnings(self) -> list[str]:
        return list(self._warnings)

    def load(self) -> OobeMatrix:
        """Read the matrix from the catalogue, enforcing the evidence rule."""
        loader = getattr(self._catalog, "oobe_matrix", None)
        if not callable(loader):
            raise OobeError(
                "the injected ImageCatalog cannot provide the OOBE matrix",
                hint="ImageCatalog.oobe_matrix() is required",
            )
        matrix = loader()
        if not isinstance(matrix, OobeMatrix):
            matrix = OobeMatrix.model_validate(matrix)
        records = [self._enforce_evidence(record) for record in matrix.records]
        return matrix.model_copy(update={"records": records})

    def record_for(self, image_id: str) -> OobeRecord | None:
        requested = str(image_id or "").strip()
        if not requested:
            return None
        matrix = self.load()
        return matrix.get(requested)

    # -- writes ----------------------------------------------------------
    def upsert(self, record: OobeRecord) -> OobeRecord:
        """Persist *record* through the catalogue and return the stored form."""
        if not isinstance(record, OobeRecord):
            raise OobeError("OOBE records must be avdfleet.core.models.OobeRecord instances")
        stored = self._stamp_evidence(record)
        writer = getattr(self._catalog, "update_oobe_record", None)
        if not callable(writer):
            raise OobeError(
                "the injected ImageCatalog cannot persist OOBE records",
                hint="ImageCatalog.update_oobe_record(record) is required",
            )
        try:
            writer(stored)
        except Exception as exc:  # noqa: BLE001 - surface as a service error
            raise OobeError(
                f"failed to persist the OOBE record for image {stored.image_id!r}: {redact(str(exc))}",
                hint="check that the data directory is writable",
            ) from exc
        return stored

    # -- evidence --------------------------------------------------------
    def _enforce_evidence(self, record: OobeRecord) -> OobeRecord:
        if record.status is not OobeStatus.VERIFIED:
            return self._stamp_evidence(record)
        missing = self._missing_evidence(record)
        if not missing:
            return self._stamp_evidence(record)
        note = (
            "downgraded from verified to unverified: missing "
            + " and ".join(missing)
            + " evidence"
        )
        warning = f"OOBE record for image {record.image_id!r} lacked {', '.join(missing)}; marked unverified"
        if warning not in self._warnings:
            self._warnings.append(warning)
            self._logger.warning("%s", warning)
        return record.model_copy(
            update={
                "status": OobeStatus.UNVERIFIED,
                "notes": _append_note(record.notes, note),
                "checked_at": record.checked_at or utcnow(),
            }
        )

    @staticmethod
    def _missing_evidence(record: OobeRecord) -> list[str]:
        missing: list[str] = []
        if not (record.activity or "").strip():
            missing.append("top activity")
        if not (record.screenshot_path or "").strip():
            missing.append("screenshot")
        return missing

    @staticmethod
    def _stamp_evidence(record: OobeRecord) -> OobeRecord:
        evidence = list(record.evidence)
        for candidate in (record.screenshot_path,):
            if candidate and candidate not in evidence:
                evidence.append(candidate)
        if evidence == record.evidence:
            return record
        return record.model_copy(update={"evidence": evidence})


def _append_note(notes: str, extra: str) -> str:
    text = (notes or "").strip()
    addition = (extra or "").strip()
    if not addition:
        return text
    return f"{text}; {addition}" if text else addition


__all__ = ["GOLDEN_SNAPSHOT", "OobeMatrixRegistry"]
