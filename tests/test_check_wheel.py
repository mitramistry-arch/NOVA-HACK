"""Tests for scripts/check_wheel.py, the packaging gate run in CI."""

import importlib.util
import zipfile
from pathlib import Path
from types import ModuleType

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_wheel.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_wheel", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_wheel = _load()

GOOD_REQUIRES = (
    "Requires-Dist: pydantic<3,>=2.7",
    "Requires-Dist: numpy<3,>=1.24",
    "Requires-Dist: fastapi<1,>=0.110",
    "Requires-Dist: httpx<1,>=0.27",
    "Requires-Dist: httpcore<2,>=1.0",
    "Requires-Dist: cryptography<47,>=42",
    "Requires-Dist: pytest<9,>=8 ; extra == 'dev'",
)


def _wheel(
    path: Path,
    extra_files: tuple[str, ...] = (),
    drop: tuple[str, ...] = (),
    requires: tuple[str, ...] = GOOD_REQUIRES,
) -> Path:
    members = [m for m in check_wheel.REQUIRED_FILES if m not in drop]
    with zipfile.ZipFile(path, "w") as zf:
        for name in (*members, *extra_files):
            zf.writestr(name, "")
        metadata = "Metadata-Version: 2.1\nName: nova\nVersion: 0.1.0\n" + "\n".join(requires)
        zf.writestr("nova-0.1.0.dist-info/METADATA", metadata + "\n")
    return path


def test_intended_wheel_passes(tmp_path: Path) -> None:
    """A wheel with runtime modules, runtime deps, and dev tools only in extras is accepted."""
    assert check_wheel.check(_wheel(tmp_path / "ok.whl")) == []


def test_shipped_benchmark_and_tests_are_rejected(tmp_path: Path) -> None:
    """Development directories must not leak into the wheel."""
    wheel = _wheel(tmp_path / "bad.whl", extra_files=("benchmark/runner.py", "tests/test_x.py"))
    problems = check_wheel.check(wheel)
    assert any("benchmark/runner.py" in p for p in problems)
    assert any("tests/test_x.py" in p for p in problems)


def test_missing_runtime_module_is_rejected(tmp_path: Path) -> None:
    """A wheel missing a required runtime module fails."""
    wheel = _wheel(tmp_path / "missing.whl", drop=("nova/store/schema.py",))
    assert any("schema.py" in p for p in check_wheel.check(wheel))


def test_dev_tool_in_runtime_requirements_is_rejected(tmp_path: Path) -> None:
    """pytest and Ruff must not be unconditional runtime requirements."""
    leaky = (*GOOD_REQUIRES, "Requires-Dist: ruff")
    problems = check_wheel.check(_wheel(tmp_path / "leaky.whl", requires=leaky))
    assert any("ruff" in p for p in problems)


def test_missing_runtime_dependency_is_rejected(tmp_path: Path) -> None:
    """Dropping a real runtime dependency from metadata fails."""
    wheel = _wheel(tmp_path / "nodeps.whl", requires=GOOD_REQUIRES[:2])
    assert any("fastapi" in p for p in check_wheel.check(wheel))
