# Contributing

Thank you for contributing to Academic Intelligence. This document covers local setup, style, commits, and pull requests.

## Development environment

### Prerequisites

- Python **3.11+**
- Git
- Optional: SerpAPI / Semantic Scholar keys for live integration tests

### Setup

```bash
git clone https://github.com/paper-research-crawler/academic-intelligence.git
cd academic-intelligence
python -m venv .venv

# Windows
.venv\Scripts\activate
# Unix
# source .venv/bin/activate

pip install -e ".[dev,docs]"
```

### Useful commands

```bash
# Tests
pytest

# Lint / format (ruff)
ruff check .
ruff format .

# Type check
mypy academic_intelligence

# Docs
mkdocs serve
mkdocs build --strict
```

## Code style

- **Formatter / linter**: Ruff (`line-length = 100`, see `pyproject.toml`)
- **Types**: Prefer complete annotations; `mypy` runs in strict mode for the package
- **Docstrings**: Google style (used by mkdocstrings)
- **Async**: I/O-bound source and storage methods should be `async`
- **Models**: Use Pydantic models in `core/models.py` / `core/types.py`; validate at boundaries

### Naming

| Kind | Convention |
|------|------------|
| Modules / packages | `snake_case` |
| Classes | `PascalCase` |
| Functions / methods | `snake_case` |
| Constants | `UPPER_SNAKE` |
| Source plugin `name` | short URL-safe string (`openalex`) |

### Structure rules

- Keep domain logic out of `cli.py`
- Sources must attach `Evidence` to every returned model
- Do not commit secrets, `.db` dumps with private data, or API keys
- Do not modify `docs/superpowers/` design archives unless the maintainers ask

## Commit messages

Prefer Conventional Commits style:

```text
feat: add OpenAlex author search pagination
fix: handle Semantic Scholar 429 with Retry-After
docs: document CLI query year ranges
test: cover JSON storage batch save
refactor: share DOI normalization helper
chore: bump dev dependencies
```

Guidelines:

- Imperative mood (“add” not “added”)
- Scope optional: `feat(sources): ...`
- Reference issues when applicable

## Pull request process

1. **Branch** from the default branch with a descriptive name (`feat/arxiv-source`, `fix/sqlite-query-year`)
2. **Implement** with tests for new behavior
3. **Run** `pytest`, `ruff check .`, and `mkdocs build` locally
4. **Open a PR** with:
   - Summary of the change
   - Motivation / linked issue
   - Test plan (commands you ran)
   - Notes on API or config changes
5. **Review**: address feedback; keep PRs focused (one concern per PR when possible)
6. **Merge**: maintainers merge after CI (when configured) and review approval

### PR checklist

- [ ] Tests added or updated
- [ ] Public API documented (docstrings + MkDocs if user-facing)
- [ ] No secrets in the diff
- [ ] Changelog note for user-visible changes (`docs/changelog.md`)

## Reporting issues

Include:

- Python version and OS
- Library version (`academic_intelligence.__version__`)
- Minimal repro (code or CLI)
- Whether network/API keys were involved
- Full traceback (redact keys)

## Code of conduct (short)

- Be respectful and constructive
- Assume good intent
- Do not use the project for scraping that violates laws or ToS

## Related

- [Architecture](architecture.md)
- [Testing](testing.md)
