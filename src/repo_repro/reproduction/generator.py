"""One model call, one candidate, explicit validation and immutable generation evidence."""

import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import ValidationError

from repo_repro.adapters.contracts import AdapterSelection
from repo_repro.adapters.pytest_prompt import PYTEST_GENERATION_PROMPT as SYSTEM_PROMPT
from repo_repro.adapters.registry import default_selection
from repo_repro.ai.base import AIProvider, ProviderError
from repo_repro.core.exceptions import RepoReproError
from repo_repro.core.models import ArtifactRef
from repo_repro.reproduction.investigation_models import CandidateFeedback, InvestigationPlan
from repo_repro.reproduction.models import GeneratedTest, ReproductionInput, TestProposal
from repo_repro.reproduction.planner import validate_input
from repo_repro.storage.artifacts import FileArtifactStore

__all__ = ["SYSTEM_PROMPT", "TestGenerator"]


class TestGenerator:
    __test__ = False

    def __init__(
        self,
        artifacts: FileArtifactStore,
        provider: AIProvider | None = None,
        *,
        adapters: AdapterSelection | None = None,
    ) -> None:
        self.artifacts = artifacts
        self.provider = provider
        self.adapters = adapters or default_selection()
        self.last_record: ArtifactRef | None = None
        self.last_candidate_code: str | None = None

    def generate(
        self,
        context: ReproductionInput,
        *,
        feedback: CandidateFeedback | None = None,
        diagnosis: InvestigationPlan | None = None,
    ) -> GeneratedTest:
        if self.provider is None:
            raise ValueError("select an AI provider explicitly")
        return self._generate(context, None, feedback=feedback, diagnosis=diagnosis)

    def seal_code(self, context: ReproductionInput, code: str) -> GeneratedTest:
        proposal = TestProposal.model_validate(
            {
                "understanding": "Caller supplied a reproduction test.",
                "plan": context.oracle.behavior_contract,
                "closest_test": None,
                "test_code": code,
                "evidence": [{"source": "issue.body", "quote": context.oracle.issue_quote}],
            }
        )
        return self._generate(context, proposal)

    def _generate(
        self,
        context: ReproductionInput,
        supplied: TestProposal | None,
        *,
        feedback: CandidateFeedback | None = None,
        diagnosis: InvestigationPlan | None = None,
    ) -> GeneratedTest:
        identity = uuid4()
        self.last_record = None
        self.last_candidate_code = None
        redact = self.provider.redact if self.provider else str
        input_record = self.artifacts.put_bytes(
            redact(context.model_dump_json()).encode(), media_type="application/json"
        )
        record: dict[str, object] = {
            "id": str(identity),
            "started_at": datetime.now(UTC).isoformat(),
            "origin": "user" if supplied else "ai",
            "input": input_record.model_dump(),
            "status": "rejected",
            "prompt_version": "1.0",
        }
        if feedback is not None:
            record["feedback"] = feedback.model_dump(mode="json")
        if diagnosis is not None:
            record["diagnosis"] = diagnosis.model_dump(mode="json")
        try:
            validate_input(context, self.adapters)
            if supplied is None:
                assert self.provider is not None
                request: object = context.model_dump(mode="json")
                if feedback is not None or diagnosis is not None:
                    request = {
                        "context": request,
                        "diagnosis": diagnosis.model_dump() if diagnosis else None,
                        "feedback": feedback.model_dump(mode="json") if feedback else None,
                    }
                schema = TestProposal.model_json_schema()
                schema["properties"]["closest_test"]["enum"] = [
                    None,
                    *(item.path for item in context.existing_tests),
                ]
                schema["$defs"]["EvidenceQuote"]["properties"]["source"]["enum"] = [
                    "issue.body",
                    *(item.path for item in context.relevant_source_files),
                    *(item.path for item in context.existing_tests),
                ]
                reply = self.provider.complete(
                    self.adapters.framework.generation_prompt + json.dumps(schema),
                    redact(json.dumps(request)),
                )
                record["model_audit"] = reply.audit.model_dump()
                supplied = TestProposal.model_validate_json(reply.content)
            self.last_candidate_code = supplied.test_code
            self.adapters.framework.validate_proposal(supplied, context)
            record["proposal"] = supplied.model_dump()
            record["status"] = "accepted"
        except (RepoReproError, ValidationError, ValueError) as error:
            record["error"] = redact(str(error))
            if isinstance(error, ProviderError):
                record["model_audit"] = error.audit.model_dump()
            record["finished_at"] = datetime.now(UTC).isoformat()
            self.last_record = self.artifacts.put_bytes(
                redact(json.dumps(record)).encode(), media_type="application/json"
            )
            raise
        record["finished_at"] = datetime.now(UTC).isoformat()
        reference = self.artifacts.put_bytes(
            redact(json.dumps(record)).encode(), media_type="application/json"
        )
        self.last_record = reference
        return GeneratedTest(
            id=identity,
            code=supplied.test_code,
            sha256=hashlib.sha256(supplied.test_code.encode()).hexdigest(),
            oracle=context.oracle,
            proposal=supplied,
            generation_record=reference,
            input_record=input_record,
        )
