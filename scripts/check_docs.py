"""Documentation integrity check — run in CI and locally.

Two guarantees, both aimed at the drift class where code/docs move but a
link or the index doesn't:

1. **No broken relative links.** Every ``[text](target.md)`` (and any other
   relative path) in a tracked Markdown file must resolve to a real file.
   External (``http(s)://``, ``mailto:``) and pure ``#anchor`` links are
   skipped — this checks paths, not the web or heading anchors.
2. **Every doc is registered.** Every Markdown file under ``docs/`` (except
   the two index files themselves) must be linked from
   ``docs/INDEX.md`` — so a new doc can't be added and silently orphaned.

    python -m scripts.check_docs

Exits non-zero (and prints each problem) if anything fails.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
INDEX = DOCS / "INDEX.md"

# Directories that are never our documentation (deps, caches, VCS, build).
_SKIP_DIRS = {
    ".git",
    ".venv",
    "node_modules",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "__pycache__",
    "a2z_core.egg-info",
}

# [text](target) — captures the target; we ignore the link text.
_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")

# Fenced code blocks (```...```) and inline code spans (`...`) — a link inside
# either is an illustrative example, not a real link, so we strip them first.
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`]*`")

# Targets that are not relative file paths we can resolve on disk.
_EXTERNAL_RE = re.compile(r"^(https?:|mailto:|tel:|#)")


def _markdown_files() -> list[Path]:
    """Every tracked ``.md`` file in the repo, skipping deps/caches."""
    out: list[Path] = []
    for path in ROOT.rglob("*.md"):
        if any(part in _SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        out.append(path)
    return sorted(out)


def _links(md: Path) -> list[str]:
    text = md.read_text(encoding="utf-8")
    text = _FENCE_RE.sub("", text)
    text = _INLINE_CODE_RE.sub("", text)
    return _LINK_RE.findall(text)


def check_links(files: list[Path]) -> list[str]:
    """Return a list of human-readable broken-link problems."""
    problems: list[str] = []
    for md in files:
        for raw in _links(md):
            target = raw.strip()
            if _EXTERNAL_RE.match(target):
                continue
            # Drop any #anchor; an empty remainder is a same-page link.
            path_part = target.split("#", 1)[0]
            if not path_part:
                continue
            resolved = (md.parent / path_part).resolve()
            if not resolved.exists():
                rel = md.relative_to(ROOT)
                problems.append(f"{rel}: broken link -> {target}")
    return problems


def check_index_registration() -> list[str]:
    """Every docs/*.md (except README/INDEX) must be linked from INDEX.md."""
    if not INDEX.exists():
        return [f"missing {INDEX.relative_to(ROOT)}"]

    linked: set[Path] = set()
    for raw in _links(INDEX):
        target = raw.strip().split("#", 1)[0]
        if not target or _EXTERNAL_RE.match(target):
            continue
        linked.add((INDEX.parent / target).resolve())

    exempt = {(DOCS / "README.md").resolve(), INDEX.resolve()}
    problems: list[str] = []
    for md in DOCS.rglob("*.md"):
        if any(part in _SKIP_DIRS for part in md.parts):
            continue
        if md.resolve() in exempt:
            continue
        if md.resolve() not in linked:
            problems.append(f"{md.relative_to(ROOT)}: not linked from docs/INDEX.md")
    return sorted(problems)


def main() -> int:
    files = _markdown_files()
    problems = check_links(files) + check_index_registration()
    if problems:
        print(f"Documentation check FAILED ({len(problems)} problem(s)):\n")
        for p in problems:
            print(f"  - {p}")
        print("\nFix the link, or register the new doc in docs/INDEX.md.")
        return 1
    print(f"Documentation check OK — {len(files)} Markdown files, all links resolve.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
