"""Portable access to explicitly provisioned synthetic QMD test assets."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil


class QmdFixtureError(RuntimeError):
    """The explicitly configured synthetic QMD fixture is incomplete."""


class QmdFixtureUnconfigured(QmdFixtureError):
    """No optional synthetic QMD fixture root was configured."""


def pytest_addoption(parser) -> None:
    """Bridge the fixture root through the canonical runner's hermetic env."""
    parser.addoption(
        "--qmd-test-root",
        help="Absolute preprovisioned synthetic QMD fixture root (no downloads)",
    )


def pytest_configure(config) -> None:
    root = config.getoption("--qmd-test-root")
    if root:
        os.environ["QMD_TEST_ROOT"] = root


@dataclass(frozen=True)
class QmdFixture:
    root: Path
    bun: str
    qmd: Path
    store: Path
    models: Path
    semantic_settings: Path


def resolve_qmd_fixture(
    *, require_store: bool = False, require_models: bool = False,
    require_semantic_settings: bool = False,
) -> QmdFixture:
    """Resolve preprovisioned assets without consulting production config or downloading."""
    raw_root = os.environ.get("QMD_TEST_ROOT")
    if not raw_root:
        raise QmdFixtureUnconfigured(
            "QMD_TEST_ROOT is not set; optional real-QMD fixture tests are disabled"
        )
    root = Path(raw_root)
    if not root.is_absolute():
        raise QmdFixtureError("QMD_TEST_ROOT must be an absolute path")

    bun = shutil.which("bun")
    qmd = root / "qmd-package/node_modules/@tobilu/qmd/dist/cli/qmd.js"
    store = root / "qmd-package/node_modules/@tobilu/qmd/dist/store.js"
    models = root / "models-verified.json"
    semantic_settings = root / "semantic-settings.json"
    required = [qmd]
    if require_store:
        required.append(store)
    if require_models:
        required.append(models)
    if require_semantic_settings:
        required.append(semantic_settings)
    missing = [str(path) for path in required if not path.is_file()]
    if bun is None:
        missing.append("bun executable on PATH")
    if missing:
        raise QmdFixtureError(
            "configured QMD_TEST_ROOT is missing required assets: " + ", ".join(missing)
        )
    return QmdFixture(root, bun, qmd, store, models, semantic_settings)


def require_qmd_fixture(**requirements) -> QmdFixture:
    """Skip only when unconfigured; fail when an explicit fixture is invalid."""
    import pytest

    try:
        return resolve_qmd_fixture(**requirements)
    except QmdFixtureUnconfigured as exc:
        pytest.skip(str(exc))
    except QmdFixtureError as exc:
        pytest.fail(str(exc), pytrace=False)
