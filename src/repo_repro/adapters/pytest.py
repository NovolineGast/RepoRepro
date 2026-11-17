"""The existing pytest implementation behind the trusted test-framework contract."""

import ast

from repo_repro.adapters.contracts import (
    PhaseEvidence,
    SessionEvidence,
    TestBundle,
)
from repo_repro.adapters.pytest_prompt import PYTEST_GENERATION_PROMPT
from repo_repro.adapters.pytest_runtime import injected_files
from repo_repro.adapters.python_safety import check_test_code, validate_proposal
from repo_repro.core.exceptions import InputError
from repo_repro.core.models import CommandSpec
from repo_repro.reproduction.models import NODE_ID, PytestEvidence, ReproductionInput, TestProposal


class PytestAdapter:
    id = "pytest"
    language_id = "python"
    display_name = "pytest"
    generation_prompt = PYTEST_GENERATION_PROMPT
    capsule_versions: tuple[str, ...] = ("1.0",)

    def validate_context(self, context: ReproductionInput) -> None:
        if self.id not in context.environment.test_frameworks:
            raise InputError("the first reproduction engine requires pytest")
        if context.oracle.node_id != NODE_ID:
            raise InputError("pytest v1 requires the sealed reproduction node ID")

    def validate_code(self, code: str) -> None:
        check_test_code(code)

    def validate_proposal(self, proposal: TestProposal, context: ReproductionInput) -> None:
        validate_proposal(proposal, context)

    def canonical_test(self, code: str) -> str:
        return ast.dump(ast.parse(code), include_attributes=False)

    def bundle(self, code: str) -> TestBundle:
        return TestBundle(
            language_id=self.language_id,
            framework_id=self.id,
            command=CommandSpec(argv=("python", "-I", "/workspace/repo_repro_generated/launch.py")),
            files=injected_files(code),
            test_path="repo_repro_generated/test_repro.py",
            report_path="/tmp/repo-repro-pytest.json",
            probe_argv=(
                "/usr/local/bin/python3",
                "-I",
                "-c",
                "import json,pytest;print(json.dumps({'version':pytest.__version__}))",
            ),
            version_key="pytest_version",
        )

    def normalize_evidence(self, report: bytes) -> SessionEvidence:
        evidence = PytestEvidence.model_validate_json(report)
        return SessionEvidence(
            framework_id=self.id,
            collected=evidence.collected,
            collection_complete=evidence.collection_complete,
            collection_errors=evidence.collection_errors,
            phases=tuple(
                PhaseEvidence(
                    target_id=item.node_id,
                    phase=item.phase,
                    outcome=item.outcome,
                    expected_failure=item.was_xfail,
                    exception_type=item.exception_type,
                    message=item.message,
                    truncated=item.truncated,
                )
                for item in evidence.phases
            ),
            session_complete=evidence.session_complete,
            exit_code=evidence.exit_code,
        )
