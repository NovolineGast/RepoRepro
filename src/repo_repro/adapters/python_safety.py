"""Conservative AST checks for the first, deliberately small pytest generation surface."""

import ast
import re

from repo_repro.core.exceptions import PolicyViolation
from repo_repro.reproduction.models import ReproductionInput, TestProposal

_BLOCKED_MODULES = frozenset(
    {
        "os",
        "sys",
        "subprocess",
        "socket",
        "requests",
        "urllib",
        "http",
        "httpx",
        "ctypes",
        "importlib",
        "pickle",
        "marshal",
        "shutil",
        "pathlib",
        "builtins",
        "inspect",
        "pytest",
        "random",
        "secrets",
        "time",
        "datetime",
        "uuid",
        "threading",
        "multiprocessing",
        "asyncio",
    }
)
_BLOCKED_CALLS = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "open",
        "__import__",
        "getattr",
        "setattr",
        "delattr",
        "globals",
        "locals",
        "vars",
        "input",
        "breakpoint",
        "exit",
        "quit",
    }
)


def check_test_code(code: str) -> None:
    """This reduces accidental unsafe generation; the sandbox remains mandatory."""
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError, RecursionError) as error:
        raise PolicyViolation(
            "generated test is not valid bounded Python", stage="generation"
        ) from error
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    if len(functions) != 1 or functions[0].name != "test_repro":
        raise PolicyViolation("generate exactly one test_repro function", stage="generation")
    function = functions[0]
    if (
        not function.body
        or not isinstance(function.body[-1], ast.Assert)
        or sum(isinstance(statement, ast.Assert) for statement in function.body) != 1
    ):
        raise PolicyViolation("the test must end with one unconditional behavior assertion")
    if (
        function.decorator_list
        or function.returns is not None
        or function.args.args
        or function.args.kwonlyargs
        or (function.args.vararg or function.args.kwarg or function.args.posonlyargs)
    ):
        raise PolicyViolation("fixtures, decorators and parameterized tests are outside this MVP")
    imported_names: set[str] = set()
    for statement in tree.body:
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            for alias in statement.names:
                name = alias.asname or (
                    alias.name.split(".")[0] if isinstance(statement, ast.Import) else alias.name
                )
                if name in imported_names or name == function.name:
                    raise PolicyViolation("import bindings must be unique")
                imported_names.add(name)
    for statement in function.body:
        if isinstance(statement, ast.Assign):
            if any(not isinstance(target, ast.Name) for target in statement.targets):
                raise PolicyViolation("assignments may only bind local result names")
            if any(
                isinstance(target, ast.Name) and target.id in imported_names
                for target in statement.targets
            ):
                raise PolicyViolation("repository imports cannot be rebound")
        elif not isinstance(statement, ast.Assert):
            raise PolicyViolation(
                "only straight-line result assignments and an assertion are supported"
            )
    if not any(isinstance(node, ast.Assert) for node in ast.walk(function)):
        raise PolicyViolation("generated test must assert correct behavior")
    for top_node in tree.body:
        if not isinstance(top_node, (ast.Import, ast.ImportFrom, ast.FunctionDef)) and not (
            isinstance(top_node, ast.Expr)
            and isinstance(top_node.value, ast.Constant)
            and isinstance(top_node.value.value, str)
        ):
            raise PolicyViolation("only imports and the test function may appear at module scope")
    for node in ast.walk(tree):
        if isinstance(
            node,
            (
                ast.While,
                ast.For,
                ast.AsyncFor,
                ast.With,
                ast.AsyncWith,
                ast.Try,
                ast.Raise,
                ast.ClassDef,
                ast.AsyncFunctionDef,
                ast.Lambda,
                ast.Global,
                ast.Nonlocal,
                ast.If,
                ast.IfExp,
                ast.Return,
                ast.Yield,
                ast.YieldFrom,
                ast.Await,
                ast.BoolOp,
                ast.NamedExpr,
                ast.ListComp,
                ast.SetComp,
                ast.DictComp,
                ast.GeneratorExp,
            ),
        ):
            raise PolicyViolation("generated test uses unsupported control flow")
        if isinstance(node, ast.FunctionDef) and node is not function:
            raise PolicyViolation("nested helper functions are outside this MVP")
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
            )
            if isinstance(node, ast.ImportFrom) and (
                node.level or any(alias.name == "*" for alias in node.names)
            ):
                raise PolicyViolation("relative and wildcard imports are refused")
            if any(
                name.split(".")[0] in _BLOCKED_MODULES or name.startswith("_") for name in names
            ):
                raise PolicyViolation("generated test imports a restricted module")
        if isinstance(node, ast.Name) and (node.id.startswith("__") or node.id in _BLOCKED_CALLS):
            raise PolicyViolation("generated test accesses a restricted builtin")
        if isinstance(node, ast.Attribute) and (
            node.attr.startswith("_") or node.attr in _BLOCKED_CALLS
        ):
            raise PolicyViolation("generated test accesses a restricted attribute")
        if isinstance(node, ast.Assert) and isinstance(node.test, ast.Constant):
            raise PolicyViolation("constant assertions cannot demonstrate a repository Bug")


def validate_proposal(proposal: TestProposal, context: ReproductionInput) -> None:
    check_test_code(proposal.test_code)
    sources = {
        item.path: item.content
        for item in (*context.relevant_source_files, *context.existing_tests)
    }
    sources["issue.body"] = context.issue.body
    if not any(item.source == "issue.body" for item in proposal.evidence):
        raise PolicyViolation("test proposal must cite the Issue")
    for evidence in proposal.evidence:
        if evidence.source not in sources or evidence.quote not in sources[evidence.source]:
            raise PolicyViolation("model cited evidence that is not present in the supplied input")
    if proposal.closest_test is not None and proposal.closest_test not in {
        item.path for item in context.existing_tests
    }:
        raise PolicyViolation("closest test must come from supplied existing tests")
    module_names = {
        re.sub(r"^src/", "", item.path)
        .removesuffix(".py")
        .replace("/", ".")
        .removesuffix(".__init__")
        for item in context.relevant_source_files
    }
    tree = ast.parse(proposal.test_code)
    imported: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported.update(
                alias.asname or alias.name.split(".")[0]
                for alias in node.names
                if alias.name in module_names
            )
        elif isinstance(node, ast.ImportFrom) and node.module in module_names:
            imported.update(alias.asname or alias.name for alias in node.names)
    if not imported or not any(
        isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id in imported)
            or (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in imported
            )
        )
        for node in ast.walk(tree)
    ):
        raise PolicyViolation("generated test must call supplied repository code")
    function = next(item for item in tree.body if isinstance(item, ast.FunctionDef))

    def depends_on_source(expression: ast.AST, tracked: set[str]) -> bool:
        for element in ast.walk(expression):
            if isinstance(element, ast.Name) and element.id in tracked:
                return True
            if isinstance(element, ast.Call) and (
                (isinstance(element.func, ast.Name) and element.func.id in imported)
                or (
                    isinstance(element.func, ast.Attribute)
                    and isinstance(element.func.value, ast.Name)
                    and element.func.value.id in imported
                )
            ):
                return True
        return False

    tracked: set[str] = set()
    for statement in function.body:
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            names = {
                element.id
                for target in targets
                for element in ast.walk(target)
                if isinstance(element, ast.Name)
            }
            dependent = statement.value is not None and depends_on_source(statement.value, tracked)
            tracked.difference_update(names)
            if dependent:
                tracked.update(names)
        elif isinstance(statement, ast.Assert) and not depends_on_source(statement.test, tracked):
            raise PolicyViolation("the assertion must depend on the repository call's result")
