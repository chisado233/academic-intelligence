# Installation

## Requirements

- **Python** 3.11 or newer
- Network access to academic APIs (Semantic Scholar, OpenAlex, etc.)
- Optional API keys for higher rate limits (see [Configuration](configuration.md))

## Install from PyPI

```bash
pip install academic-intelligence
```

This installs the library and the `ai` CLI entry point.

## Install from source (development)

Clone the repository and install in editable mode with dev tools:

```bash
git clone https://github.com/paper-research-crawler/academic-intelligence.git
cd academic-intelligence
pip install -e ".[dev]"
```

Install documentation dependencies as well:

```bash
pip install -e ".[docs]"
# or both:
pip install -e ".[dev,docs]"
```

## Optional dependency groups

Defined in `pyproject.toml`:

| Extra | Purpose | Packages (highlights) |
|-------|---------|------------------------|
| *(default)* | Runtime | `httpx`, `pydantic`, `sqlalchemy[asyncio]`, `aiosqlite`, `typer`, `rich` |
| `dev` | Tests & lint | `pytest`, `pytest-asyncio`, `pytest-cov`, `ruff`, `mypy`, `pre-commit` |
| `docs` | MkDocs site | `mkdocs`, `mkdocs-material`, `mkdocstrings[python]`, `pymdown-extensions` |

## Verify installation

```bash
python -c "import academic_intelligence; print(academic_intelligence.__version__)"
ai --help
```

## Runtime stack

| Component | Role |
|-----------|------|
| **httpx** | Async HTTP client for source APIs |
| **pydantic** | Config and domain models |
| **SQLAlchemy + aiosqlite** | Default SQLite storage |
| **typer + rich** | CLI and terminal tables |

## Next steps

- [Quick Start](quick-start.md) — run your first collection
- [Configuration](configuration.md) — API keys and storage paths
