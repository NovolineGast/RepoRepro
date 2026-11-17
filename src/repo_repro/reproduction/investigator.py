"""Bounded AI planning and feedback, with independent execution and a sealed final test."""

import hashlib
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from repo_repro.ai.base import AIProvider, ProviderError
from repo_repro.analyzer.repository import GitRepository
from repo_repro.capsule.security import SecretRedactor
from repo_repro.core.context import IssueContext
from repo_repro.core.exceptions import InputError, PolicyViolation
from repo_repro.core.models import ArtifactRef
from repo_repro.reproduction.ai_planner import AIPlanner
from repo_repro.reproduction.executor import ReproductionExecutor
from repo_repro.reproduction.generator import TestGenerator
from repo_repro.reproduction.investigation_models import (
    CandidateFeedback,
    ExecutionObservation,
    InvestigationAttempt,
    InvestigationResult,
    InvestigationStop,
)
from repo_repro.reproduction.models import (
    ExecutionEnvironment,
    FailureOracle,
    ReproductionResult,
    ReproductionStatus,
)
from repo_repro.storage.artifacts import FileArtifactStore
from repo_repro.verification.differential import DifferentialVerifier


class AIInvestigator:
    def __init__(
        self,
        provider: AIProvider,
        executor: ReproductionExecutor,
        artifacts: FileArtifactStore,
    ) -> None:
        self.provider = provider
        self.executor = executor
        self.artifacts = artifacts
        self.adapters = executor.adapters
        self.generator = TestGenerator(artifacts, provider, adapters=self.adapters)
        self.redactor = SecretRedactor()

    def _clean(self, value: str, limit: int) -> str:
        cleaned = self.provider.redact(self.redactor.text(value))
        if self.redactor.binary_sensitive(cleaned.encode()):
            return "[sensitive feedback omitted]"
        return cleaned[:limit]

    def _feedback(self, number: int, result: ReproductionResult) -> CandidateFeedback:
        def excerpt(reference: ArtifactRef | None, limit: int) -> str:
            if reference is None:
                return ""
            # Redact complete values before clipping, including keys spanning the excerpt boundary.
            data = self.artifacts.read_bytes(reference)
            return self._clean(data.decode("utf-8", errors="replace"), limit)

        observations = tuple(
            ExecutionObservation(
                exit_code=item.exit_code,
                termination=item.termination,
                stdout=excerpt(item.stdout, 4000),
                stderr=excerpt(item.stderr, 4000),
                pytest_evidence=excerpt(item.test_report, 12000),
            )
            for item in result.executions[:2]
        )
        return CandidateFeedback(
            attempt=number,
            previous_test=self._clean(result.generated_test.code, 32768),
            status=result.status,
            reason=self._clean(result.reason, 4000),
            observations=observations,
        )

    def run(
        self,
        issue: IssueContext,
        repository_path: Path,
        oracle: FailureOracle,
        environment: ExecutionEnvironment,
        *,
        ref: str = "HEAD",
        max_candidates: int = 3,
        fixed_commit: str | None = None,
        initial_code: str | None = None,
        max_plan_attempts: int = 2,
    ) -> InvestigationResult:
        if not 1 <= max_candidates <= 5:
            raise InputError("max_candidates must be between 1 and 5")
        if not 1 <= max_plan_attempts <= 2:
            raise InputError("max_plan_attempts must be between 1 and 2")
        with GitRepository().open(repository_path, ref=ref) as view:
            original = view.commit
        if fixed_commit is not None:
            with GitRepository().open(repository_path, ref=fixed_commit) as view:
                fixed_commit = view.commit
            if original == fixed_commit:
                raise InputError("original and fixed commits must differ")
        planned = AIPlanner(self.artifacts, self.provider, adapters=self.adapters).plan(
            issue, repository_path, oracle, ref=original, max_attempts=max_plan_attempts
        )
        calls = planned.model_calls
        attempts: list[InvestigationAttempt] = []
        feedback: CandidateFeedback | None = None
        selected: ReproductionResult | None = None
        seen: set[str] = set()
        stop: InvestigationStop = "budget_exhausted"
        for number in range(1, max_candidates + 1):
            feedback_record = (
                self.artifacts.put_bytes(
                    feedback.model_dump_json().encode(),
                    media_type="application/json",
                )
                if feedback is not None
                else None
            )
            try:
                if number == 1 and initial_code is not None:
                    generated = self.generator.seal_code(planned.context, initial_code)
                else:
                    calls += 1
                    generated = self.generator.generate(
                        planned.context, feedback=feedback, diagnosis=planned.plan
                    )
            except ProviderError as error:
                attempts.append(
                    InvestigationAttempt(
                        number=number,
                        rejection=self._clean(str(error), 4000),
                        feedback_record=feedback_record,
                        generation_record=self.generator.last_record,
                    )
                )
                stop = "model_error"
                break
            except (ValidationError, PolicyViolation) as error:
                reason = self._clean(str(error), 4000)
                attempts.append(
                    InvestigationAttempt(
                        number=number,
                        rejection=reason,
                        feedback_record=feedback_record,
                        generation_record=self.generator.last_record,
                    )
                )
                feedback = CandidateFeedback(
                    attempt=number,
                    validation_error=reason,
                    reason="candidate rejected before execution",
                    previous_test=(
                        self._clean(self.generator.last_candidate_code, 32768)
                        if self.generator.last_candidate_code is not None
                        else None
                    ),
                )
                continue
            canonical = hashlib.sha256(
                self.adapters.framework.canonical_test(generated.code).encode()
            ).hexdigest()
            if canonical in seen:
                attempts.append(
                    InvestigationAttempt(
                        number=number,
                        generated=generated,
                        rejection="duplicate candidate; no execution",
                        feedback_record=feedback_record,
                        generation_record=generated.generation_record,
                    )
                )
                stop = "duplicate_candidate"
                break
            seen.add(canonical)
            result = self.executor.run(
                repository_path, planned.context.repository.current_commit, generated, environment
            )
            attempts.append(
                InvestigationAttempt(
                    number=number,
                    generated=generated,
                    result=result,
                    feedback_record=feedback_record,
                    generation_record=generated.generation_record,
                )
            )
            selected = result
            if result.status is ReproductionStatus.REPRODUCED:
                stop = "reproduced"
                break
            if result.status is ReproductionStatus.ENVIRONMENT_ERROR:
                stop = "environment_error"
                break
            if result.status is ReproductionStatus.TIMEOUT:
                stop = "timeout"
                break
            if result.flaky:
                stop = "flaky"
                break
            if not result.assessments or any(
                item.reason
                not in {
                    "target executed and passed",
                    "test failed for a different reason",
                }
                for item in result.assessments
            ):
                stop = "incomplete_evidence"
                break
            feedback = self._feedback(number, result)
        session = InvestigationResult(
            id=uuid4(),
            plan=planned.plan,
            plan_record=planned.record,
            attempts=tuple(attempts),
            selected_result=selected,
            model_calls=calls,
            max_candidates=max_candidates,
            max_plan_attempts=max_plan_attempts,
            stop_reason=stop,
        )
        self.artifacts.put_bytes(
            self.provider.redact(session.model_dump_json()).encode(), media_type="application/json"
        )
        # Fixed-version evidence never enters a model prompt or candidate repair decision.
        if stop == "reproduced" and selected is not None and fixed_commit is not None:
            verification = DifferentialVerifier(self.executor, self.artifacts).verify(
                repository_path,
                selected.commit,
                fixed_commit,
                selected.generated_test,
                environment,
            )
            session = session.model_copy(update={"verification": verification})
            self.artifacts.put_bytes(
                self.provider.redact(session.model_dump_json()).encode(),
                media_type="application/json",
            )
        return session
