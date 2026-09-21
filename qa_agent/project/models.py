"""Immutable data shapes for the Project Discovery Engine (Phase F Part 1,
docs/19-project-discovery-engine.md).

Every value this package produces must be traceable to a real file on disk -
`DetectedItem.evidence` is the one mechanism that guarantees that: nothing in
`detectors.py` or `knowledge.py` is allowed to construct a `DetectedItem`
without at least one real evidence path, and every dataclass here is frozen
(tuples, not lists; a `MappingProxyType`, not a plain dict, for `evidence`)
so a caller can never mutate a result after `discover_project()` returns it -
the same "hand back a fact, not a mutable scratch pad" guarantee `Finding`
already gives the deterministic analyzer pipeline (adapters.py).

Status values are plain string constants, not an `enum.Enum` - matching this
project's existing convention everywhere else a closed set of run-outcome
labels is needed (schemas.py's `STATUS_*`, validator.py's, decision.py's,
repair_loop.py's), not a new pattern invented for this package.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Optional, Tuple

# discover_project() always returns one of these - never raises to a caller,
# per this package's one hard rule (discovery.py's own module docstring).
STATUS_SUCCESS = "success"
STATUS_PARTIAL_SUCCESS = "partial_success"
STATUS_INVALID_ROOT = "invalid_root"
STATUS_PERMISSION_DENIED = "permission_denied"
STATUS_DISCOVERY_FAILED = "discovery_failed"

STATUSES = (
    STATUS_SUCCESS,
    STATUS_PARTIAL_SUCCESS,
    STATUS_INVALID_ROOT,
    STATUS_PERMISSION_DENIED,
    STATUS_DISCOVERY_FAILED,
)

# Evidence lists are capped per DetectedItem so a repository with thousands
# of `.py` files doesn't turn "Python was detected" into a thousand-line
# dump - presence is still checked across the *entire* walk, unabridged;
# only the *displayed* evidence sample is capped. Named here, once, rather
# than as a magic number buried in detectors.py.
MAX_EVIDENCE_PER_ITEM = 5


@dataclass(frozen=True)
class DetectedItem:
    """One verified fact - a language, a framework, a package manager, or a
    build system - plus the real file(s) that prove it.

    `evidence` is never empty: a `DetectedItem` with no evidence is exactly
    the "invented fact" this whole package exists to refuse (see the module
    docstring above and detectors.py's `_detected` helper, the one place
    every `DetectedItem` in this package is actually constructed).
    """

    name: str
    evidence: Tuple[str, ...]

    def __post_init__(self):
        if not self.evidence:
            raise ValueError(
                "DetectedItem({!r}) constructed with no evidence - every "
                "detected fact must be backed by at least one real file".format(self.name)
            )


def _frozen_evidence_map(raw):
    """A read-only view over a plain dict - `MappingProxyType` rather than a
    stdlib immutable-mapping type (there isn't one) or a new dependency, so
    `ProjectKnowledge.evidence`/`ProjectDiscoveryResult.errors`-style fields
    stay genuinely unwritable from outside this package without adding
    anything beyond the standard library.
    """
    return MappingProxyType(dict(raw))


@dataclass(frozen=True)
class ProjectKnowledge:
    """The permanent knowledge model of one repository - every fact any
    future runtime-QA phase (Phase G onward) should consume instead of
    re-walking the filesystem itself.

    Every sequence field is a tuple (never a list) and `evidence` is a
    `MappingProxyType` (never a plain dict) so this object is immutable in
    fact, not just by convention - a caller cannot `.append()` a finding
    into it the way `RunResult` deliberately *can* be built up field by
    field during a still-running `runner.run()`. `ProjectKnowledge` is only
    ever constructed once, fully formed, at the end of `discover_project()`
    (knowledge.py's `build_knowledge()`).

    `repository_type` and `application_type` are single classification
    strings, not `DetectedItem`s - each one is a conclusion drawn from
    several files at once, not one fact tied to one file, so their
    supporting evidence lives in the shared `evidence` mapping instead
    (keyed `"repository_type"`/`"application_type"`) rather than forcing an
    artificial single-file `DetectedItem` shape onto a multi-file
    conclusion.
    """

    root_path: str
    repository_type: str
    application_type: str
    languages: Tuple[DetectedItem, ...] = ()
    frameworks: Tuple[DetectedItem, ...] = ()
    package_managers: Tuple[DetectedItem, ...] = ()
    build_systems: Tuple[DetectedItem, ...] = ()
    workspace_structure: Tuple[str, ...] = ()
    monorepo_packages: Tuple[str, ...] = ()
    important_directories: Tuple[str, ...] = ()
    important_files: Tuple[str, ...] = ()
    configuration_files: Tuple[str, ...] = ()
    runtime_files: Tuple[str, ...] = ()
    documentation_files: Tuple[str, ...] = ()
    specification_files: Tuple[str, ...] = ()
    dependency_files: Tuple[str, ...] = ()
    environment_files: Tuple[str, ...] = ()
    docker_files: Tuple[str, ...] = ()
    ci_files: Tuple[str, ...] = ()
    test_directories: Tuple[str, ...] = ()
    entry_points: Tuple[str, ...] = ()
    ignored_paths: Tuple[str, ...] = ()
    evidence: Mapping[str, Tuple[str, ...]] = field(default_factory=lambda: _frozen_evidence_map({}))


@dataclass(frozen=True)
class ProjectDiscoveryResult:
    """The one return type of `discover_project()`. `project` is `None`
    exactly when discovery could not even begin (`INVALID_ROOT`,
    `PERMISSION_DENIED` on the root itself, or an unexpected internal
    failure - `DISCOVERY_FAILED`); for `SUCCESS` and `PARTIAL_SUCCESS` it is
    always a fully-formed `ProjectKnowledge`, never partially built.
    """

    status: str
    project: Optional[ProjectKnowledge]
    errors: Tuple[str, ...] = ()
    warnings: Tuple[str, ...] = ()
    elapsed_time: float = 0.0
    scanned_files: int = 0
    ignored_files: int = 0
