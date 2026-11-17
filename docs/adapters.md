# Adapter architecture

The development branch routes the existing Python/pytest implementation through explicit adapters. This is an architectural refactor; it does not add a supported language or framework.

```bash
repro adapters
```

The built-in registry currently lists `python` and `pytest` only. Unknown identifiers fail rather than importing a module, installing a plugin, or selecting a fallback.

## Responsibilities

`LanguageAdapter` provides project detection, read-only analysis, source selection and path checks, planning instructions, runtime inventory, and runtime fingerprint fields. `PythonLanguageAdapter` uses the existing Python analyzer and preserves its committed-tree behavior.

`TestFrameworkAdapter` provides generation instructions, context/proposal/code validation, a deterministic execution bundle, candidate canonicalization, and native-report normalization. `PytestAdapter` owns the existing AST checks, launcher layout, pytest availability probe, and pytest report parser.

The core owns model/candidate budgets, execution identity, sandbox policy and enforcement, repetition, generic evidence rules, differential comparison, storage, and cleanup. Adapters supply data and validation; they are not given a backend execution callback through the framework interface.

`TestBundle` contains the command, immutable support-file mapping, selected test path, temporary report path, and framework probe. Bundle contents stay in the reserved support directory. The Docker backend resolves a registered pair, reconstructs its bundle, and checks that the injected test bytes match the sealed candidate before executing it. Kernel enforcement and adapter runtime inventory are probed separately; inventory cannot replace kernel evidence.

Framework-native reports normalize into `SessionEvidence` / `PhaseEvidence`. The verifier still checks target identity, collection/session completeness, setup/call/teardown, skipped/expected failures, truncation, and exit consistency. A different framework identifier is rejected. Normalization is not permission to invent missing native evidence.

## Selecting adapters in Python

```python
from repo_repro.adapters.registry import builtin_registry
from repo_repro.reproduction.executor import ReproductionExecutor
from repo_repro.reproduction.generator import TestGenerator
from repo_repro.sandbox.docker_runner import DockerSandboxRunner
from repo_repro.storage.artifacts import FileArtifactStore

# artifacts is an existing pathlib.Path; image_id is a reviewed local immutable image ID.
registry = builtin_registry()
selection = registry.resolve("python", "pytest")
store = FileArtifactStore(artifacts)
runner = DockerSandboxRunner(
    image=image_id, artifacts=store, registry=registry, adapters=selection
)
generator = TestGenerator(store, adapters=selection)  # add a provider for model generation
executor = ReproductionExecutor(
    runner, store, snapshot_backend=runner, adapters=selection
)
```

Use the same selection for generation and execution. `RepositoryAnalysis(language=selection.language)`, `build_input(..., adapters=selection)`, and `AIPlanner(..., adapters=selection)` expose the corresponding analysis/context hooks. `AIInvestigator` inherits its executor's selection.

A reviewed local implementation can be added with `register_language()` / `register_framework()` on a registry instance. Registration rejects duplicate IDs, missing language registrations, incompatible pairs, and invalid identifiers. Registries are instance-local; built-in discovery creates a fresh registry. There is no automatic entry-point or repository/Capsule plugin loading.

## Trust and compatibility

Adapters are trusted host-side Python code, not a security boundary. Review them like the rest of the application. A malicious adapter can lie about evidence or execute host code. Model output and Capsule metadata never authorize installing or importing one.

Existing `PythonAnalyzer`, pytest backend/request names, `reproduction.safety`, `reproduction.runtime`, and `SYSTEM_PROMPT` imports remain available. Existing backend implementations with `run_test()` / `run_snapshot()` are supported; bundle-capable backends receive the explicit bundle instead. The v1 result wire field `pytest_report` remains unchanged, with `ExecutionResult.test_report` providing neutral internal access.

Capsule v1 remains a Python/pytest codec with its existing schema, paths, command, helper hashes, and fingerprint layout. It binds to compatible local adapters and rejects other pairs. Golden tests retain the pre-refactor helper hashes, and a Capsule captured before the refactor was replayed with matching behavior and environment.

Some compatibility boundaries intentionally remain Python-specific: v1 `SourceFile` / `FailureOracle` / investigation-path schemas, the Capsule v1 layout, and the Docker backend's Python supervisor. A future language needs a versioned data/codec migration, native validation/report collection, approved runtime images, and integration tests. Registering a name alone does not establish language support.

## Testing an implementation

Keep import/discovery free of network requests, Docker calls, workspace creation, and target-code execution. Exercise registration failures, wrong-framework evidence, unsafe or nondeterministic bundles, inventory collisions, and preserved sandbox policy. Use real sandbox tests before claiming execution support.

The test suite includes a contract-only framework double to prove that generation, bundle execution, and report parsing actually dispatch through the interfaces. That double is not registered as a built-in and is not an implementation of an additional language/framework.
