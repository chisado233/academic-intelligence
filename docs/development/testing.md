# Testing

## Strategy

| Layer | What | Tools |
|-------|------|-------|
| Unit | Models, processors, utils, pure logic | `pytest` |
| Storage | SQLite / JSON backends with temp paths | `pytest` + tmp_path |
| CLI / import | Entry points and package exports | `pytest` |
| Integration / network | Live APIs | Marked `@pytest.mark.network` (opt-in) |

Goals for v0.1:

- Fast default suite (no live network)
- High coverage on models, processors, storage, utils
- Async tests via `pytest-asyncio` (`asyncio_mode = auto`)

## Running tests

```bash
# From repository root, with package installed editable
pip install -e ".[dev]"

# Full default suite (includes coverage via addopts)
pytest

# Verbose
pytest -v

# One file
pytest tests/test_models.py

# Skip slow tests
pytest -m "not slow"

# Network tests only (requires keys / network)
pytest -m network
```

Coverage reports (configured in `pyproject.toml`):

- Terminal: missing lines
- HTML: `htmlcov/`
- XML: `coverage.xml`

```bash
# Open HTML report after a run
# Windows: start htmlcov/index.html
```

## Writing tests

### Layout

```text
tests/
├── test_models.py
├── test_processors.py
├── test_storage.py
├── test_utils.py
├── test_import_and_cli.py
└── ...
```

### Conventions

- File names: `test_*.py`
- Prefer small, focused test functions
- Use `tmp_path` for filesystem backends
- Build models with valid `Evidence` fixtures
- Do not call live APIs unless marked `network`

### Example: model validation

```python
from academic_intelligence.core.models import Evidence, Paper
from academic_intelligence.core.types import SourceType

def test_paper_normalizes_doi():
    evidence = Evidence(
        source=SourceType.OPENALEX,
        source_url="https://api.openalex.org/",
        confidence=0.9,
    )
    paper = Paper(
        title="Example",
        doi="https://doi.org/10.1234/example",
        evidence=evidence,
    )
    assert paper.doi == "10.1234/example"
```

### Example: async storage

```python
import pytest
from academic_intelligence.storage.sqlite_store import SQLiteStorage

@pytest.mark.asyncio
async def test_sqlite_roundtrip(tmp_path):
    store = SQLiteStorage(str(tmp_path / "t.db"))
    await store.connect()
    try:
        # save / query ...
        ...
    finally:
        await store.close()
```

### Markers

Defined in `pyproject.toml`:

| Marker | Meaning |
|--------|---------|
| `slow` | Long-running tests |
| `integration` | Cross-module integration |
| `network` | Requires network access |

```python
import pytest

@pytest.mark.network
async def test_openalex_live():
    ...
```

## Coverage expectations

| Area | Target (guideline) |
|------|--------------------|
| Overall package | Prefer **≥ 80%** on non-CLI modules |
| `core.models` / processors / storage | High priority — aim for **≥ 90%** where practical |
| `cli.py` | Often omitted from coverage (see `tool.coverage.run.omit`) |

Failing coverage is not always a merge blocker in early alpha, but new code should ship with tests that exercise success and important failure paths.

## Continuous integration (recommended)

Suggested CI steps:

```bash
pip install -e ".[dev,docs]"
ruff check .
mypy academic_intelligence
pytest
mkdocs build --strict
```

## Related

- [Contributing](contributing.md)
- [Architecture](architecture.md)
