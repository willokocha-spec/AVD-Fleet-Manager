"""Declarative fleet engine: ``fleet.yaml`` loading, planning and reconciliation.

Public API
----------
``FleetLoader``
    Parse YAML files/strings/mappings into :class:`~avdfleet.core.models.FleetSpec`,
    validate against ``contracts/fleet.schema.json``, resolve proxy credentials from
    the environment and expand instance defaults.
``FleetPlanner``
    Diff a fleet spec against injected managers and return a
    :class:`~avdfleet.core.models.FleetPlan` (create/update/delete/noop).
``FleetReconciler``
    Apply a plan idempotently through injected ``AvdManager``/``ProxyPool``/
    ``ProxyAssigner`` services, emit ``fleet.applied`` and report partial failures.

The CLI (``avdfleet fleet plan|apply``) and the REST API
(``POST /api/v1/fleet/plan`` and ``POST /api/v1/fleet/apply``) both drive this
package through :class:`~avdfleet.core.interfaces.FleetReconciler`.
"""

from __future__ import annotations

from .loader import (
    IMAGE_POLICIES,
    FleetLoader,
    ResolvedFleet,
    load_fleet,
    load_fleet_dict,
    load_fleet_file,
    load_fleet_text,
    validate_fleet,
)
from .planner import FleetPlanner, FleetState, build_fleet_plan
from .reconciler import FleetReconciler, FleetReconcilerService, Reconciler
from .schema import (
    FLEET_API_VERSION,
    FLEET_KIND,
    FLEET_SCHEMA_ENV,
    FLEET_SCHEMA_RELATIVE,
    SchemaIssue,
    fleet_document_error,
    fleet_schema_example,
    fleet_schema_path,
    format_issues,
    load_fleet_schema,
    raise_for_document,
    validate_document,
)

__all__ = [
    "FLEET_API_VERSION",
    "FLEET_KIND",
    "FLEET_SCHEMA_ENV",
    "FLEET_SCHEMA_RELATIVE",
    "IMAGE_POLICIES",
    "FleetLoader",
    "FleetPlanner",
    "FleetReconciler",
    "FleetReconcilerService",
    "FleetState",
    "Reconciler",
    "ResolvedFleet",
    "SchemaIssue",
    "build_fleet_plan",
    "fleet_document_error",
    "fleet_schema_example",
    "fleet_schema_path",
    "format_issues",
    "load_fleet",
    "load_fleet_dict",
    "load_fleet_file",
    "load_fleet_schema",
    "load_fleet_text",
    "raise_for_document",
    "validate_document",
    "validate_fleet",
]
