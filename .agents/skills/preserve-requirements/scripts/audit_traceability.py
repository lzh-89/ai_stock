#!/usr/bin/env python3
"""Validate atomic requirement traceability manifests using only the standard library."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


ORDER = {
    "proposed": 0,
    "confirmed": 1,
    "implementing": 2,
    "implemented_unverified": 3,
    "verified": 4,
    "merged": 5,
    "archived": 6,
}
STATUSES = set(ORDER) | {"cancelled"}
SURFACES = {"backend", "frontend", "data", "docs"}
EXTERNAL_PREFIXES = ("http://", "https://", "git:", "run:", "manual:")


class Audit:
    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.ids: set[str] = set()

    def error(self, location: str, message: str) -> None:
        self.errors.append(f"{location}: {message}")

    def warning(self, location: str, message: str) -> None:
        self.warnings.append(f"{location}: {message}")

    def require_list(self, value: Any, location: str) -> list[Any]:
        if not isinstance(value, list):
            self.error(location, "must be a list")
            return []
        return value

    def check_id(self, value: Any, location: str) -> str:
        if not isinstance(value, str) or not value.strip():
            self.error(location, "must be a non-empty string")
            return ""
        value = value.strip()
        if value in self.ids:
            self.error(location, f"duplicate ID {value}")
        self.ids.add(value)
        return value

    def check_ref(self, value: Any, location: str) -> None:
        if not isinstance(value, str) or not value.strip():
            self.error(location, "must be a non-empty reference string")
            return
        ref = value.strip()
        if ref.startswith(EXTERNAL_PREFIXES):
            return
        path_text = ref.split("#", 1)[0].split("::", 1)[0]
        path = Path(path_text)
        if path.is_absolute():
            self.error(location, "local references must be repository-relative")
            return
        if not (self.repo_root / path).exists():
            self.error(location, f"referenced path does not exist: {path_text}")

    def check_refs(self, value: Any, location: str) -> list[Any]:
        refs = self.require_list(value, location)
        for index, ref in enumerate(refs):
            self.check_ref(ref, f"{location}[{index}]")
        return refs


def non_empty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def audit_manifest(path: Path) -> Audit:
    repo_root = Path.cwd().resolve()
    audit = Audit(repo_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        audit.error(str(path), f"cannot read valid JSON: {exc}")
        return audit

    if not isinstance(data, dict):
        audit.error("manifest", "root must be an object")
        return audit
    if data.get("schemaVersion") != 1:
        audit.error("schemaVersion", "must equal 1")
    for field in ("changeId", "title"):
        if not non_empty_text(data.get(field)):
            audit.error(field, "must be a non-empty string")

    source_documents = audit.check_refs(data.get("sourceDocuments"), "sourceDocuments")
    source_set = {str(ref).split("#", 1)[0].split("::", 1)[0] for ref in source_documents if isinstance(ref, str)}
    requirements = audit.require_list(data.get("requirements"), "requirements")
    if not requirements:
        audit.error("requirements", "must contain at least one requirement")

    for req_index, requirement in enumerate(requirements):
        req_loc = f"requirements[{req_index}]"
        if not isinstance(requirement, dict):
            audit.error(req_loc, "must be an object")
            continue
        req_id = audit.check_id(requirement.get("id"), f"{req_loc}.id")
        label = req_id or req_loc
        if not non_empty_text(requirement.get("statement")):
            audit.error(f"{label}.statement", "must be a non-empty atomic requirement statement")
        req_status = requirement.get("status")
        if req_status not in STATUSES:
            audit.error(f"{label}.status", f"must be one of {sorted(STATUSES)}")

        source_refs = audit.require_list(requirement.get("sourceRefs"), f"{label}.sourceRefs")
        if not source_refs:
            audit.error(f"{label}.sourceRefs", "must contain at least one source excerpt")
        source_ref_ids: set[str] = set()
        for ref_index, source_ref in enumerate(source_refs):
            ref_loc = f"{label}.sourceRefs[{ref_index}]"
            if not isinstance(source_ref, dict):
                audit.error(ref_loc, "must be an object")
                continue
            source_id = audit.check_id(source_ref.get("id"), f"{ref_loc}.id")
            if source_id:
                source_ref_ids.add(source_id)
            source_path = source_ref.get("path")
            audit.check_ref(source_path, f"{ref_loc}.path")
            normalized_path = str(source_path).split("#", 1)[0].split("::", 1)[0]
            if normalized_path not in source_set:
                audit.error(f"{ref_loc}.path", "must also appear in sourceDocuments")
            for field in ("locator", "excerpt"):
                if not non_empty_text(source_ref.get(field)):
                    audit.error(f"{ref_loc}.{field}", "must be a non-empty string")

        decision_refs = audit.check_refs(requirement.get("decisionRefs", []), f"{label}.decisionRefs")
        requirement_refs = audit.check_refs(requirement.get("requirementDocRefs", []), f"{label}.requirementDocRefs")
        implementation_refs = audit.check_refs(requirement.get("implementationDocRefs", []), f"{label}.implementationDocRefs")
        release_refs = audit.check_refs(requirement.get("releaseRefs", []), f"{label}.releaseRefs")
        rollback_refs = audit.check_refs(requirement.get("rollbackRefs", []), f"{label}.rollbackRefs")

        criteria = audit.require_list(requirement.get("criteria"), f"{label}.criteria")
        if not criteria:
            audit.error(f"{label}.criteria", "must contain atomic acceptance criteria")
        active_statuses: list[str] = []
        for criterion_index, criterion in enumerate(criteria):
            criterion_loc = f"{label}.criteria[{criterion_index}]"
            if not isinstance(criterion, dict):
                audit.error(criterion_loc, "must be an object")
                continue
            criterion_id = audit.check_id(criterion.get("id"), f"{criterion_loc}.id")
            criterion_label = criterion_id or criterion_loc
            if not non_empty_text(criterion.get("statement")):
                audit.error(f"{criterion_label}.statement", "must be a non-empty, independently observable statement")
            status = criterion.get("status")
            if status not in STATUSES:
                audit.error(f"{criterion_label}.status", f"must be one of {sorted(STATUSES)}")
                continue
            if status != "cancelled":
                active_statuses.append(status)

            linked_source_ids = audit.require_list(criterion.get("sourceRefIds"), f"{criterion_label}.sourceRefIds")
            if not linked_source_ids:
                audit.error(f"{criterion_label}.sourceRefIds", "must link at least one source excerpt")
            for source_id in linked_source_ids:
                if source_id not in source_ref_ids:
                    audit.error(f"{criterion_label}.sourceRefIds", f"unknown source reference ID: {source_id}")

            surfaces = audit.require_list(criterion.get("appliesTo"), f"{criterion_label}.appliesTo")
            unknown_surfaces = set(surfaces) - SURFACES if all(isinstance(item, str) for item in surfaces) else set()
            if unknown_surfaces:
                audit.error(f"{criterion_label}.appliesTo", f"unknown surfaces: {sorted(unknown_surfaces)}")
            if not surfaces:
                audit.error(f"{criterion_label}.appliesTo", "must name at least one surface")

            evidence = criterion.get("evidence")
            if not isinstance(evidence, dict):
                audit.error(f"{criterion_label}.evidence", "must be an object")
                evidence = {}
            evidence_refs: dict[str, list[Any]] = {}
            for field in ("artifacts", "tests", "uiAcceptance", "dataAcceptance", "reviews"):
                evidence_refs[field] = audit.check_refs(evidence.get(field, []), f"{criterion_label}.evidence.{field}")

            if status == "cancelled":
                if not non_empty_text(criterion.get("cancellationReason")):
                    audit.error(f"{criterion_label}.cancellationReason", "is required for cancelled criteria")
                if not decision_refs:
                    audit.error(f"{criterion_label}", "cancelled criteria require parent decisionRefs")
                continue

            rank = ORDER.get(status, -1)
            if rank >= ORDER["confirmed"] and not decision_refs:
                audit.error(criterion_label, "confirmed or later criteria require decisionRefs")
            if rank >= ORDER["implementing"]:
                if not requirement_refs:
                    audit.error(criterion_label, "implementing or later criteria require requirementDocRefs")
                if not implementation_refs:
                    audit.error(criterion_label, "implementing or later criteria require implementationDocRefs")
            if rank >= ORDER["implemented_unverified"] and not evidence_refs["artifacts"]:
                audit.error(criterion_label, "implemented or later criteria require artifact evidence")
            if rank >= ORDER["verified"]:
                if "backend" in surfaces and not evidence_refs["tests"]:
                    audit.error(criterion_label, "verified backend criteria require automated tests")
                if "frontend" in surfaces:
                    if not evidence_refs["tests"]:
                        audit.error(criterion_label, "verified frontend criteria require automated tests")
                    if not evidence_refs["uiAcceptance"]:
                        audit.error(criterion_label, "verified frontend criteria require browser acceptance evidence")
                if "data" in surfaces and not evidence_refs["dataAcceptance"]:
                    audit.error(criterion_label, "verified data criteria require data acceptance evidence")
                if "docs" in surfaces and not evidence_refs["reviews"]:
                    audit.error(criterion_label, "verified documentation criteria require review evidence")
                if not rollback_refs:
                    audit.error(criterion_label, "verified or later criteria require rollbackRefs")
            if rank >= ORDER["merged"] and not release_refs:
                audit.error(criterion_label, "merged or later criteria require releaseRefs")

        if active_statuses and req_status in ORDER:
            least_status = min(active_statuses, key=lambda item: ORDER[item])
            if req_status != least_status:
                audit.error(f"{label}.status", f"must equal least advanced active criterion status: {least_status}")
        elif criteria and req_status != "cancelled" and not active_statuses:
            audit.error(f"{label}.status", "must be cancelled when every criterion is cancelled")
        if len(criteria) == 1 and non_empty_text(requirement.get("statement")):
            statement = requirement["statement"]
            if any(token in statement for token in (" and ", "、", "以及", "并且", "同时")):
                audit.warning(label, "single criterion may hide multiple observable behaviors; review atomicity")

    return audit


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: audit_traceability.py <manifest.json>", file=sys.stderr)
        return 2
    manifest_path = Path(sys.argv[1])
    audit = audit_manifest(manifest_path)
    for warning in audit.warnings:
        print(f"WARNING: {warning}")
    for error in audit.errors:
        print(f"ERROR: {error}")
    if audit.errors:
        print(f"FAILED: {len(audit.errors)} error(s), {len(audit.warnings)} warning(s)")
        return 1
    print(f"PASSED: 0 errors, {len(audit.warnings)} warning(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

