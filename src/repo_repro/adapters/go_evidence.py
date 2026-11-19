"""Normalize complete native Go events without inventing pytest lifecycle phases."""

import json
import re

from repo_repro.adapters.contracts import PhaseEvidence, SessionEvidence


def normalize(report: bytes) -> SessionEvidence:
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate Go evidence key")
            result[key] = value
        return result

    data = json.loads(report, object_pairs_hook=unique)
    if (
        not isinstance(data, dict)
        or data.get("schema_version") != "2.0"
        or data.get("framework") != "go-test"
        or data.get("truncated") is not False
        or type(data.get("exit_code")) is not int
        or data["exit_code"] not in (0, 1)
    ):
        raise ValueError("invalid Go report envelope")
    events = data.get("events")
    if not isinstance(events, list) or not 1 <= len(events) <= 2000:
        raise ValueError("missing Go event stream")
    package: str | None = None
    started = False
    terminal: str | None = None
    ran = False
    outcome: str | None = None
    output: list[str] = []
    errors: list[str] = []
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("Action"), str):
            raise ValueError("invalid Go event")
        # Recent toolchains may emit build events as well as test events.
        action = event["Action"]
        if action.startswith("build-"):
            if action == "build-fail":
                errors.append("Go build failed")
            continue
        current = event.get("Package")
        if not isinstance(current, str) or not current.endswith("/repo_repro_generated"):
            raise ValueError("wrong Go package identity")
        if package is not None and current != package:
            raise ValueError("mixed Go package identities")
        package = current
        target = event.get("Test")
        if terminal is not None:
            raise ValueError("events follow package completion")
        if action == "start" and target is None and not started:
            started = True
        elif not started:
            raise ValueError("Go session did not start")
        elif target is not None:
            if target != "TestRepro":
                raise ValueError("unexpected target or subtest")
            if action == "run" and not ran:
                ran = True
            elif action == "output" and ran and outcome is None:
                value = event.get("Output")
                if not isinstance(value, str):
                    raise ValueError("invalid Go output")
                output.append(value)
            elif action in {"pass", "fail", "skip"} and ran and outcome is None:
                outcome = action
            else:
                raise ValueError("duplicate or out-of-order Go target event")
        elif action in {"pass", "fail", "skip"}:
            terminal = action
            if event.get("FailedBuild"):
                errors.append("Go build failed")
        elif action != "output":
            raise ValueError("unexpected Go package event")
    complete = terminal is not None and (terminal == "pass") == (data["exit_code"] == 0)
    if errors or (terminal == "fail" and not ran):
        errors.append("Go compilation, dependency or initialization failure")
    message = "".join(output)
    # Only the sealed candidate's Errorf location can establish an assertion failure.
    assertion = re.search(r"(?m)^\s+repro_test\.go:\d+: (.+)$", message)
    exception = "GoAssertionFailure" if assertion and "panic:" not in message else None
    phase = PhaseEvidence(
        "TestRepro",
        "call",
        {"pass": "passed", "fail": "failed", "skip": "skipped"}.get(outcome or "", "skipped"),
        False,
        exception,
        assertion[1] if assertion else message,
        False,
    )
    return SessionEvidence(
        framework_id="go-test",
        collected=("TestRepro",) if ran else (),
        collection_complete=complete,
        collection_errors=tuple(errors),
        phases=(phase,) if outcome else (),
        session_complete=complete,
        exit_code=data["exit_code"],
        required_phases=("call",),
    )
