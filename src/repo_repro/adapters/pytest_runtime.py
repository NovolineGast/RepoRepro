"""Trusted runtime resources; never import pytest or generated code on the host."""

from importlib.resources import files

LAUNCHER = """import importlib.util, sys
import pytest
plugin_path = '/workspace/repo_repro_generated/plugin.py'
spec = importlib.util.spec_from_file_location('repo_repro_collector', plugin_path)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)
sys.path[:0] = ['/workspace', '/workspace/src']
raise SystemExit(pytest.main([
    '-q', '-c', '/workspace/repo_repro_generated/pytest.ini', '--rootdir=/workspace',
    '--noconftest', '--assert=plain', '-o', 'addopts=',
    'repo_repro_generated/test_repro.py::test_repro',
], plugins=[plugin]))
"""


def injected_files(code: str) -> dict[str, bytes]:
    return {
        "repo_repro_generated/test_repro.py": code.encode(),
        "repo_repro_generated/plugin.py": files("repo_repro.reproduction")
        .joinpath("pytest_plugin.txt")
        .read_bytes(),
        "repo_repro_generated/launch.py": LAUNCHER.encode(),
        "repo_repro_generated/pytest.ini": b"[pytest]\naddopts =\n",
    }
