"""Pure evidence rules; no model, network, or execution backend participates here."""

import hashlib
import json
import re

from pydantic import ValidationError

from repo_repro.adapters.contracts import TestFrameworkAdapter
from repo_repro.adapters.registry import default_selection
from repo_repro.core.models import ExecutionResult, Termination
from repo_repro.reproduction.models import (
    AttemptAssessment,
    FailureOracle,
    ReproductionStatus,
)


def assess_execution(
    result: ExecutionResult,
    oracle: FailureOracle,
    report: bytes | None,
    *,
    framework: TestFrameworkAdapter | None = None,
) -> AttemptAssessment:
    selected = framework or default_selection().framework
    name = selected.display_name

    def verdict(
        status: ReproductionStatus, reason: str, signature: str | None = None
    ) -> AttemptAssessment:
        return AttemptAssessment(
            execution_id=result.id, status=status, reason=reason, failure_signature=signature
        )

    if result.termination is Termination.TIMEOUT:
        return verdict(ReproductionStatus.TIMEOUT, "sandbox deadline exceeded")
    if result.termination in {
        Termination.INFRA_ERROR,
        Termination.OOM,
        Termination.WORKSPACE_LIMIT,
    }:
        return verdict(ReproductionStatus.ENVIRONMENT_ERROR, result.termination.value)
    if result.termination is not Termination.EXITED or not result.logs_complete:
        return verdict(
            ReproductionStatus.INCONCLUSIVE, "interrupted or incomplete execution evidence"
        )
    if result.exit_code not in {0, 1}:
        return verdict(
            ReproductionStatus.ENVIRONMENT_ERROR,
            f"{name} did not complete an ordinary test session",
        )
    try:
        evidence = selected.normalize_evidence(report or b"")
    except (ValidationError, ValueError, RecursionError):
        return verdict(
            ReproductionStatus.INCONCLUSIVE, f"missing or malformed structured {name} evidence"
        )
    if evidence.framework_id != selected.id:
        return verdict(
            ReproductionStatus.INCONCLUSIVE, "test evidence belongs to another framework"
        )
    if (
        not evidence.collection_complete
        or not evidence.session_complete
        or evidence.exit_code != result.exit_code
    ):
        return verdict(
            ReproductionStatus.INCONCLUSIVE,
            f"{name} session/exit evidence is incomplete or contradictory",
        )
    if evidence.collection_errors:
        return verdict(ReproductionStatus.ENVIRONMENT_ERROR, f"{name} collection failed")
    if evidence.collected != (oracle.node_id,):
        return verdict(
            ReproductionStatus.INCONCLUSIVE, "the selected target was not collected exactly once"
        )
    phases = evidence.phases
    if any(phase.truncated for phase in phases):
        return verdict(ReproductionStatus.INCONCLUSIVE, "failure evidence was truncated")
    if len(phases) != len(evidence.required_phases) or {phase.phase for phase in phases} != set(
        evidence.required_phases
    ):
        return verdict(ReproductionStatus.INCONCLUSIVE, f"missing or duplicate {name} phases")
    if any(
        phase.target_id != oracle.node_id or phase.expected_failure or phase.outcome == "skipped"
        for phase in phases
    ):
        return verdict(ReproductionStatus.INCONCLUSIVE, "skipped, xfail or wrong target")
    if any(phase.phase != "call" and phase.outcome != "passed" for phase in phases):
        return verdict(ReproductionStatus.ENVIRONMENT_ERROR, "fixture setup/teardown failed")
    call = next(phase for phase in phases if phase.phase == "call")
    if call.outcome == "passed" and result.exit_code == 0:
        return verdict(ReproductionStatus.NOT_REPRODUCED, "target executed and passed")
    if call.outcome != "failed" or result.exit_code != 1:
        return verdict(ReproductionStatus.INCONCLUSIVE, "call outcome and exit code disagree")
    normalized = re.sub(r"0x[0-9a-fA-F]+", "<address>", call.message or "")
    signature = hashlib.sha256(json.dumps([call.exception_type, normalized]).encode()).hexdigest()
    if call.exception_type == oracle.exception_type and oracle.message_contains in (
        call.message or ""
    ):
        return verdict(
            ReproductionStatus.REPRODUCED,
            "target call failure matches the sealed Issue oracle",
            signature,
        )
    return verdict(ReproductionStatus.INCONCLUSIVE, "test failed for a different reason", signature)
