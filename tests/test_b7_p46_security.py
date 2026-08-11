"""B7-P46-R28 (FIX-AA / C-2) tests: write-path traversal guard.

C-2  Config ``storage_path`` / ``cache_path`` accepted relative ``..`` and
     absolute paths without any validation, so a poisoned configuration could
     write SQLite / JSON data (or cache files) to arbitrary writable
     locations outside the working directory (verified live in P13 S1 and
     re-verified at the start of this round).  The fix validates write paths
     at the Config layer: the *normalized* form is checked for a leading
     ``..`` segment (catches ``sub/../../evil.db`` too), while the original
     value is preserved so legitimate relative-to-cwd paths and deliberate
     absolute user paths keep working byte-for-byte.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from academic_intelligence import AcademicIntelligence, Config
from academic_intelligence.core.models import AuthorRef, Paper

# ---------------------------------------------------------------------------
# C-2: write paths must not escape the working directory
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "../evil.db",
        "..\\evil.db",
        "../../tmp/evil.db",
        "../../tmp/jsevil",  # JSON backend directory
        "sub/../../evil.db",  # normalization collapses to leading ..
        "..",
        "../",
        "./../evil.db",
        "",
        "   ",
    ],
)
def test_write_path_rejects_cwd_escape(bad: str) -> None:
    """Relative paths that escape the cwd are rejected at Config build time."""
    with pytest.raises(ValidationError):
        Config(storage_path=bad)
    with pytest.raises(ValidationError):
        Config(cache_path=bad)


@pytest.mark.parametrize(
    "good",
    [
        "./academic_intelligence.db",  # default-style relative path
        "data/store.db",
        "sub/dir/x.db",
        "x.db",
        ":memory:",  # special sqlite value
        "./.ai_cache.json",
    ],
)
def test_write_path_accepts_relative_paths(good: str) -> None:
    """Relative paths inside the cwd keep working, byte-for-byte preserved."""
    cfg = Config(storage_path=good)
    assert cfg.storage_path == good
    cfg2 = Config(cache_path=good)
    assert cfg2.cache_path == good


@pytest.mark.parametrize(
    "abs_path",
    [
        "C:/Windows/Temp/ai_probe.db",
        "C:\\data\\store.db",
        "/tmp/ai_cache.json",
    ],
)
def test_write_path_accepts_absolute_user_paths(abs_path: str) -> None:
    """Deliberate absolute user paths are allowed and preserved (compat)."""
    cfg = Config(storage_path=abs_path)
    assert cfg.storage_path == abs_path
    cfg2 = Config(cache_path=abs_path)
    assert cfg2.cache_path == abs_path


def test_write_path_rejects_assignment_and_from_dict() -> None:
    """Validation also fires on post-construction assignment and from_dict."""
    cfg = Config()
    with pytest.raises(ValidationError):
        cfg.storage_path = "../evil.db"
    with pytest.raises(ValidationError):
        Config.from_dict({"storage_path": "../evil.db"})
    with pytest.raises(ValidationError):
        Config.from_dict({"cache_path": "..\\evil_cache.json"})


@pytest.mark.asyncio
async def test_traversal_config_is_blocked_end_to_end(tmp_path: Path) -> None:
    """The P13 S1 attack shape is blocked at the module API."""
    # A deep cwd makes the escape obvious; the traversal config must be
    # rejected before any file is created.
    deep = tmp_path / "cwd_a"
    deep.mkdir(parents=True)
    old = Path.cwd()
    import os

    os.chdir(deep)
    try:
        with pytest.raises(ValidationError):
            AcademicIntelligence(Config(storage_path="../../evil.db"))
        with pytest.raises(ValidationError):
            AcademicIntelligence(
                Config(storage_type="json", storage_path="../../jsevil")
            )
    finally:
        os.chdir(old)
    # Nothing escaped the deep dir.
    assert not (tmp_path / "evil.db").exists()
    assert not (tmp_path / "jsevil").exists()


@pytest.mark.asyncio
async def test_legit_relative_path_still_writes(tmp_path: Path) -> None:
    """A relative-to-cwd path still connects and persists (no regression)."""
    import os

    old = Path.cwd()
    os.chdir(tmp_path)
    try:
        (tmp_path / "sub").mkdir()  # SQLite does not create parent dirs
        ai = AcademicIntelligence(Config(storage_path="./sub/store.db"))
        await ai.connect()
        try:
            pid = await ai.storage.save_paper(
                Paper(
                    id="p-r28-legit",
                    title="Legit relative path",
                    authors=[AuthorRef(name="Probe Author")],
                    year=2026,
                )
            )
            assert pid == "p-r28-legit"
        finally:
            await ai.close()
        assert (tmp_path / "sub" / "store.db").exists()
    finally:
        os.chdir(old)
