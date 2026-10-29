# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``scripts/export-public.sh``: the public export tree is built from an allowlist of the committed tree.

Contract pinned here (the script header documents it for the reader):

* The source is the tree of ``HEAD`` (``git ls-tree``), never the working tree: untracked, ignored, staged or modified
  files are never exported, and the allowlist itself is read from the exported commit.
* ``scripts/export-allowlist.txt``: one rule per line, ``#`` comments.  ``path`` exports one file, ``dir/`` everything
  under it, ``!path`` / ``!dir/`` marks a path deliberately left out.  The longest matching rule wins.  A tracked path
  that matches no rule makes the script fail (nothing silently ships); a malformed, duplicate or contradictory rule
  fails too.  docs/ is default-deny (``!docs/`` plus one rule per public docs file or folder): the design log (prefix
  ``SDD`` = ``sanitize_scan.ALLOWLIST_PREFIX``) stays behind without a public file naming it, as
  test_design_log_unreferenced.py requires.  That nothing else stays behind unnoticed is pinned on this repository's
  HEAD below (only the design log and ``EXPORT_TOOLING``, the script and its allowlist).
* Agent configuration and spec/plan/Task files at any depth (``SCRIPT_REFUSES``) are refused even when a rule would
  export them; they have no rule, so a committed agent-configuration folder fails the run as uncovered.
* ``<dest>.manifest`` (next to the tree, so the tree holds only public files) lists ``<mode> <sha256> <path>`` per
  exported file, sorted by path in byte order, after a header with the commit, the file count and the tree hash.  The
  tree hash is the sha256 of the entry lines; it is printed as the last stdout line ``tree-sha256 <hex>``.  It depends
  on the exported content only, so a commit that touches only excluded paths keeps it.
* Modes are exactly 644 or 755 whatever the umask; two runs give byte-identical manifests.
* A dirty tree (tracked changes) is refused unless ``--allow-dirty`` (HEAD is still what is exported); ``--dry-run``
  lists ``export <path>`` / ``exclude <path>`` and writes nothing; ``--check`` verifies a tree against its manifest.

Fixture repositories are built in ``tmp_path`` with a copy of the script; the last tests run it on this repository.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import lib_proc
import pytest
import sanitize_scan as S

REPO = S.REPO_ROOT
SCRIPT = REPO / "scripts" / "export-public.sh"
ALLOWLIST = REPO / "scripts" / "export-allowlist.txt"
# /bin/bash is bash 3.2 on macOS: running the script with it keeps the script 3.2 compatible.
BASH = "/bin/bash" if Path("/bin/bash").is_file() else (shutil.which("bash") or "bash")
SDD = S.ALLOWLIST_PREFIX  # the design-log prefix, the one tree the export leaves behind
SCRIPT_REFUSES = r"\.claude/|\.kiro/|\.codex/|\.mcp\.json|CLAUDE\.md|AGENTS\.md|spec\.md|plan\.md|Task\.md"
INTERNAL_RE = re.compile(r"(^|/)(" + re.escape(SDD) + "|" + SCRIPT_REFUSES + ")")  # internal material at any depth
EXPORT_TOOLING = {"scripts/export-public.sh", "scripts/export-allowlist.txt"}  # maintainer tooling, kept internal
TREE_HASH_RE = re.compile(r"tree-sha256 ([0-9a-f]{64})")
ENTRY_RE = re.compile(r"(100644|100755) ([0-9a-f]{64}) (.+)")

PNG = b"\x89PNG\r\n\x1a\n\x00\x00binary\x00payload"
BASE_FILES: dict[str, tuple[str | bytes, int]] = {
    "README.md": ("# sample\n", 0o644),
    "LICENSE": ("MIT No Attribution\n", 0o644),
    ".gitignore": ("*.env\n", 0o644),
    "docs/DEPLOY.md": ("deploy\n", 0o644),
    "docs/static/arch.png": (PNG, 0o644),
    "docs/static/arch.drawio": ("<mxfile/>\n", 0o644),
    f"{SDD}plan.md": ("internal plan\n", 0o644),
    f"{SDD}notes.txt": ("internal notes\n", 0o644),
    "tools/x/run.sh": ("#!/bin/sh\necho x\n", 0o755),
    "tools/x/my file.txt": ("a path with a space\n", 0o644),
}
BASE_ALLOWLIST = """\
# fixture allowlist
README.md
LICENSE
.gitignore
!docs/          # default-deny
docs/DEPLOY.md
docs/static/
scripts/
tools/
"""
EXPORTED = {
    "README.md", "LICENSE", ".gitignore", "docs/DEPLOY.md", "docs/static/arch.png", "docs/static/arch.drawio",
    "tools/x/run.sh", "tools/x/my file.txt", "scripts/export-public.sh", "scripts/export-allowlist.txt",
}


# ----------------------------------------------------------------------------------------------------------- helpers
@dataclass
class Fixture:
    """A throw-away git repository that carries a copy of the export script and its own allowlist."""

    root: Path
    home: Path
    files: dict[str, tuple[bytes, int]] = field(default_factory=dict)

    @property
    def env(self) -> dict[str, str]:
        return {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.home), "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
        }

    def git(self, *args: str) -> str:
        return lib_proc.run(["git", *args], cwd=self.root, env=self.env, check=True, capture_output=True,
                            text=True).stdout

    def write(self, rel: str, content: str | bytes, mode: int = 0o644, *, record: bool = True) -> None:
        data = content.encode() if isinstance(content, str) else content
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        p.chmod(mode)
        if record:
            self.files[rel] = (data, mode)

    def commit(self, message: str = "change") -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "--no-gpg-sign", "-m", message)
        return self.git("rev-parse", "HEAD").strip()

    def run(self, *args: str, umask: str | None = None) -> subprocess.CompletedProcess[str]:
        script = str(self.root / "scripts" / "export-public.sh")
        cmd = [BASH, "-c", f'umask {umask}; exec "$0" "$@"', BASH, script, *args] if umask else [BASH, script, *args]
        return lib_proc.run(cmd, cwd=self.root, env=self.env, capture_output=True, text=True, check=False)


@pytest.fixture
def fx(tmp_path: Path) -> Iterator[Fixture]:
    assert SCRIPT.is_file(), "scripts/export-public.sh does not exist yet"
    root = tmp_path / "repo with space"
    root.mkdir()
    (tmp_path / "home").mkdir()
    f = Fixture(root=root, home=tmp_path / "home")
    f.git("init", "-q")
    for rel, (content, mode) in BASE_FILES.items():
        f.write(rel, content, mode)
    f.write("scripts/export-public.sh", SCRIPT.read_bytes(), 0o755)
    f.write("scripts/export-allowlist.txt", BASE_ALLOWLIST)
    f.commit("initial")
    yield f


def out(proc: subprocess.CompletedProcess[str]) -> str:
    return proc.stdout + proc.stderr


def tree_hash(proc: subprocess.CompletedProcess[str]) -> str:
    lines = proc.stdout.strip().splitlines()
    assert lines, f"no stdout:\n{out(proc)}"
    m = TREE_HASH_RE.fullmatch(lines[-1])
    assert m, f"the last stdout line must be 'tree-sha256 <hex>': {lines[-1]!r}"
    return m.group(1)


def manifest_path(dest: Path) -> Path:
    return Path(str(dest) + ".manifest")


def read_manifest(dest: Path) -> tuple[dict[str, str], list[str]]:
    """``({header key: value}, [entry lines])`` of ``<dest>.manifest``."""
    header: dict[str, str] = {}
    entries: list[str] = []
    for ln in manifest_path(dest).read_text(encoding="utf-8").splitlines():
        if ln.startswith("#"):
            key, _, value = ln.lstrip("# ").partition(" ")
            header[key] = value
        else:
            entries.append(ln)
    return header, entries


def manifest_paths(dest: Path) -> list[str]:
    paths = []
    for ln in read_manifest(dest)[1]:
        m = ENTRY_RE.fullmatch(ln)
        assert m, f"malformed manifest entry {ln!r}"
        paths.append(m.group(3))
    return paths


def files_under(dest: Path) -> set[str]:
    return {p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file() or p.is_symlink()}


def dry_run_sides(proc: subprocess.CompletedProcess[str]) -> tuple[list[str], list[str]]:
    exported = [ln[len("export "):] for ln in proc.stdout.splitlines() if ln.startswith("export ")]
    excluded = [ln[len("exclude "):] for ln in proc.stdout.splitlines() if ln.startswith("exclude ")]
    return exported, excluded


def expected_entries(fx: Fixture, paths: set[str]) -> list[str]:
    """Manifest entries computed independently from the bytes and modes the fixture committed."""
    lines = []
    for rel in sorted(paths, key=lambda s: s.encode()):
        data, mode = fx.files[rel]
        lines.append(f"{'100755' if mode & 0o100 else '100644'} {hashlib.sha256(data).hexdigest()} {rel}")
    return lines


# ------------------------------------------------------------------------------------------------- what is exported
def test_internal_trees_are_excluded_and_docs_static_is_exported(fx: Fixture, tmp_path: Path) -> None:
    dest = tmp_path / "out dir" / "tree"
    proc = fx.run(str(dest))
    assert proc.returncode == 0, out(proc)
    assert files_under(dest) == EXPORTED
    assert set(manifest_paths(dest)) == EXPORTED
    assert (dest / "docs/static/arch.png").read_bytes() == PNG
    assert not (dest / SDD).exists()
    assert not manifest_path(dest).resolve().is_relative_to(dest.resolve()), "the manifest lives next to the tree"


def test_untracked_ignored_staged_and_modified_files_are_never_exported(fx: Fixture, tmp_path: Path) -> None:
    fx.write("tools/x/untracked.txt", "untracked\n", record=False)
    fx.write("tools/x/local.env", "X=1\n", record=False)  # ignored by the committed .gitignore
    fx.write("tools/x/staged.txt", "staged only\n", record=False)
    fx.git("add", "tools/x/staged.txt")
    fx.write("README.md", "# edited in the working tree\n", record=False)
    dest = tmp_path / "tree"
    proc = fx.run("--allow-dirty", str(dest))
    assert proc.returncode == 0, out(proc)
    assert files_under(dest) == EXPORTED
    assert (dest / "README.md").read_bytes() == fx.files["README.md"][0], "HEAD content, not the working copy"


def test_untracked_files_alone_do_not_make_the_tree_dirty(fx: Fixture, tmp_path: Path) -> None:
    fx.write("tools/x/untracked.txt", "untracked\n", record=False)
    proc = fx.run(str(tmp_path / "tree"))
    assert proc.returncode == 0, out(proc)
    assert "tools/x/untracked.txt" not in files_under(tmp_path / "tree")


def test_dirty_tree_is_refused_without_allow_dirty(fx: Fixture, tmp_path: Path) -> None:
    fx.write("README.md", "# uncommitted edit\n", record=False)
    dest = tmp_path / "tree"
    proc = fx.run(str(dest))
    assert proc.returncode != 0, out(proc)
    assert "--allow-dirty" in out(proc)
    assert not dest.exists() and not manifest_path(dest).exists()


@pytest.mark.parametrize("new_path", ["newdir/a.txt", "EXTRA.md", ".claude/settings.json"])
@pytest.mark.parametrize("mode", ["export", "dry-run"])
def test_a_tracked_path_no_rule_covers_fails_and_writes_nothing(fx: Fixture, tmp_path: Path, new_path: str,
                                                                mode: str) -> None:
    fx.write(new_path, "new\n")
    fx.commit("add an unlisted path")
    dest = tmp_path / "tree"
    proc = fx.run(*(["--dry-run"] if mode == "dry-run" else []), str(dest))
    assert proc.returncode != 0, out(proc)
    assert new_path in proc.stderr, f"name the uncovered path:\n{out(proc)}"
    assert "export-allowlist.txt" in proc.stderr, "say where to add the rule"
    assert not dest.exists() and not manifest_path(dest).exists()


def test_dry_run_lists_every_tracked_path_once_and_writes_nothing(fx: Fixture, tmp_path: Path) -> None:
    dest = tmp_path / "tree"
    proc = fx.run("--dry-run", str(dest))
    assert proc.returncode == 0, out(proc)
    exported, excluded = dry_run_sides(proc)
    assert set(exported) == EXPORTED and len(exported) == len(EXPORTED)
    assert sorted(excluded) == [f"{SDD}notes.txt", f"{SDD}plan.md"]
    tracked = [p for p in fx.git("ls-files", "-z").split("\0") if p]
    assert sorted(exported + excluded) == sorted(tracked)
    assert not dest.exists() and not manifest_path(dest).exists()


def test_docs_are_default_deny(fx: Fixture, tmp_path: Path) -> None:
    fx.write("docs/new/page.md", "not reviewed for the public tree yet\n")
    fx.commit("a docs page with no rule of its own")
    dest = tmp_path / "tree"
    proc = fx.run(str(dest))
    assert proc.returncode == 0, out(proc)
    assert "docs/new/page.md" not in files_under(dest)
    assert "docs/new/page.md" in dry_run_sides(fx.run("--dry-run"))[1]


def test_the_allowlist_is_read_from_the_exported_commit(fx: Fixture, tmp_path: Path) -> None:
    before = fx.run("--dry-run")
    fx.write("scripts/export-allowlist.txt", BASE_ALLOWLIST + f"{SDD}notes.txt\n", record=False)
    after = fx.run("--dry-run")
    assert before.returncode == 0 and after.returncode == 0, out(before) + out(after)
    assert dry_run_sides(after) == dry_run_sides(before), "an uncommitted allowlist edit must not change the export"


@pytest.mark.parametrize(
    "allowlist,extra,offender",
    [
        (BASE_ALLOWLIST.replace("!docs/ ", "docs/ "), None, f"{SDD}plan.md"),
        (BASE_ALLOWLIST, "tools/y/.claude/agent.json", "tools/y/.claude/agent.json"),
        (BASE_ALLOWLIST, "tools/y/spec.md", "tools/y/spec.md"),
        (BASE_ALLOWLIST.replace("README.md\n", "README.md\nAGENTS.md\n"), "AGENTS.md", "AGENTS.md"),
    ],
    ids=["docs-wholesale", "nested-agent-config", "nested-spec", "agents-file"],
)
def test_internal_material_is_refused_even_when_a_rule_exports_it(fx: Fixture, tmp_path: Path, allowlist: str,
                                                                  extra: str | None, offender: str) -> None:
    fx.write("scripts/export-allowlist.txt", allowlist)
    if extra:
        fx.write(extra, "internal\n")
    fx.commit("allowlist exports internal material")
    dest = tmp_path / "tree"
    for args in (["--dry-run", str(dest)], [str(dest)]):
        proc = fx.run(*args)
        assert proc.returncode != 0, out(proc)
        assert offender in proc.stderr, out(proc)
    assert not dest.exists() and not manifest_path(dest).exists()


def test_the_longest_matching_rule_wins(fx: Fixture, tmp_path: Path) -> None:
    fx.write("tools/x/private/key.txt", "kept out\n")
    fx.write("scripts/export-allowlist.txt", BASE_ALLOWLIST + "!tools/x/private/\n")
    fx.commit("nested exclusion")
    dest = tmp_path / "tree"
    proc = fx.run(str(dest))
    assert proc.returncode == 0, out(proc)
    assert "tools/x/private/key.txt" not in files_under(dest)
    assert "tools/x/run.sh" in files_under(dest)


@pytest.mark.parametrize(
    "bad_rule",
    ["/README.md", "docs/../LICENSE", "tools/*.sh", "README.md", "!README.md", "LICENSE trailing-words", "./LICENSE"],
    ids=["absolute", "dot-dot", "glob", "duplicate", "contradiction", "trailing-text", "dot-slash"],
)
def test_malformed_duplicate_or_contradictory_rules_are_rejected(fx: Fixture, tmp_path: Path, bad_rule: str) -> None:
    fx.write("scripts/export-allowlist.txt", BASE_ALLOWLIST + bad_rule + "\n")
    fx.commit("bad rule")
    proc = fx.run("--dry-run")
    assert proc.returncode != 0, out(proc)
    assert bad_rule.split()[0] in proc.stderr, f"name the offending rule:\n{out(proc)}"


# ---------------------------------------------------------------------------------------- manifest and determinism
def test_manifest_matches_an_independent_computation(fx: Fixture, tmp_path: Path) -> None:
    dest = tmp_path / "tree"
    proc = fx.run(str(dest))
    assert proc.returncode == 0, out(proc)
    header, entries = read_manifest(dest)
    want = expected_entries(fx, EXPORTED)
    assert entries == want
    want_hash = hashlib.sha256("".join(ln + "\n" for ln in want).encode()).hexdigest()
    assert tree_hash(proc) == want_hash
    assert header["tree-sha256"] == want_hash
    assert header["commit"] == fx.git("rev-parse", "HEAD").strip()
    assert header["files"] == str(len(EXPORTED))


def test_two_runs_give_identical_manifests_and_tree_hash(fx: Fixture, tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    pa, pb = fx.run(str(a)), fx.run(str(b))
    assert pa.returncode == 0 and pb.returncode == 0, out(pa) + out(pb)
    assert manifest_path(a).read_bytes() == manifest_path(b).read_bytes()
    assert tree_hash(pa) == tree_hash(pb)


def test_modes_are_644_or_755_whatever_the_umask(fx: Fixture, tmp_path: Path) -> None:
    dest = tmp_path / "tree"
    proc = fx.run(str(dest), umask="077")
    assert proc.returncode == 0, out(proc)
    modes = {rel: (dest / rel).stat().st_mode & 0o7777 for rel in files_under(dest)}
    assert modes.pop("tools/x/run.sh") == 0o755
    assert modes.pop("scripts/export-public.sh") == 0o755
    assert set(modes.values()) == {0o644}, modes


def test_tree_hash_depends_on_the_exported_content_only(fx: Fixture, tmp_path: Path) -> None:
    first = fx.run(str(tmp_path / "t1"))
    fx.write(f"{SDD}plan.md", "the design log moved on\n")
    fx.commit("excluded path only")
    second = fx.run(str(tmp_path / "t2"))
    fx.write("README.md", "# public change\n")
    fx.commit("public change")
    third = fx.run(str(tmp_path / "t3"))
    assert all(p.returncode == 0 for p in (first, second, third)), out(first) + out(second) + out(third)
    assert tree_hash(first) == tree_hash(second)
    assert read_manifest(tmp_path / "t1")[0]["commit"] != read_manifest(tmp_path / "t2")[0]["commit"]
    assert tree_hash(third) != tree_hash(first)


def test_a_non_empty_destination_is_refused(fx: Fixture, tmp_path: Path) -> None:
    dest = tmp_path / "tree"
    dest.mkdir()
    (dest / "keep.txt").write_text("mine\n")
    proc = fx.run(str(dest))
    assert proc.returncode != 0, out(proc)
    assert (dest / "keep.txt").read_text() == "mine\n" and files_under(dest) == {"keep.txt"}
    assert not manifest_path(dest).exists()


# -------------------------------------------------------------------------------------------------------- --check
def test_check_accepts_a_fresh_export(fx: Fixture, tmp_path: Path) -> None:
    dest = tmp_path / "tree"
    made = fx.run(str(dest))
    checked = fx.run("--check", str(dest))
    assert made.returncode == 0 and checked.returncode == 0, out(made) + out(checked)
    assert tree_hash(checked) == tree_hash(made)


def _modify(dest: Path) -> str:
    (dest / "docs/DEPLOY.md").write_text("tampered\n")
    return "docs/DEPLOY.md"


def _chmod(dest: Path) -> str:
    (dest / "LICENSE").chmod(0o755)
    return "LICENSE"


def _delete(dest: Path) -> str:
    (dest / "tools/x/my file.txt").unlink()
    return "tools/x/my file.txt"


def _add(dest: Path) -> str:
    (dest / "docs/static/new.txt").write_text("not in the manifest\n")
    return "docs/static/new.txt"


def _edit_manifest(dest: Path) -> str:
    m = manifest_path(dest)
    lines = m.read_text().splitlines()
    i = next(n for n, ln in enumerate(lines) if ln.endswith(" README.md"))
    mode, _digest, rel = lines[i].split(" ", 2)
    lines[i] = f"{mode} {'0' * 64} {rel}"
    m.write_text("\n".join(lines) + "\n")
    return "README.md"


@pytest.mark.parametrize("tamper", [_modify, _chmod, _delete, _add, _edit_manifest],
                         ids=["modified", "mode", "deleted", "extra", "manifest-edited"])
def test_check_detects_a_tree_that_differs_from_its_manifest(fx: Fixture, tmp_path: Path,
                                                            tamper: Callable[[Path], str]) -> None:
    dest = tmp_path / "tree"
    assert fx.run(str(dest)).returncode == 0
    rel = tamper(dest)
    proc = fx.run("--check", str(dest))
    assert proc.returncode != 0, out(proc)
    assert rel in out(proc), f"name the differing path:\n{out(proc)}"


def test_check_without_a_manifest_fails(fx: Fixture, tmp_path: Path) -> None:
    dest = tmp_path / "tree"
    dest.mkdir()
    proc = fx.run("--check", str(dest))
    assert proc.returncode != 0 and "manifest" in out(proc), out(proc)


# ------------------------------------------------------------------------------------------------- interface
@pytest.mark.parametrize("args", [[], ["--check"], ["--dry-run", "--check", "x"], ["--bogus", "x"], ["a", "b"]],
                         ids=["no-args", "check-without-dest", "two-modes", "unknown-option", "two-dests"])
def test_usage_errors_exit_2(fx: Fixture, args: list[str]) -> None:
    proc = fx.run(*args)
    assert proc.returncode == 2, out(proc)
    assert "usage" in proc.stderr.lower(), out(proc)


def test_script_header_documents_the_uses_and_the_script_is_clean() -> None:
    assert SCRIPT.is_file(), "scripts/export-public.sh does not exist yet"
    text = SCRIPT.read_text(encoding="utf-8")
    header = "\n".join(ln for ln in text.splitlines()[:60] if ln.startswith("#")).lower()
    for use in ("clean-room", "scan", "freeze", "single-commit push", "--check", "--dry-run", "--allow-dirty",
                "export-allowlist.txt", "tree-sha256"):
        assert use in header, f"the script header must explain {use!r}"
    assert os.access(SCRIPT, os.X_OK), "the script is executable"
    proc = lib_proc.run([BASH, "-n", str(SCRIPT)], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck not installed")
    proc = lib_proc.run(["shellcheck", "-x", "--shell=bash", str(SCRIPT)], capture_output=True, text=True,
                        check=False)
    assert proc.returncode == 0, proc.stdout


def test_allowlist_lists_docs_subfolders_explicitly_and_excludes_the_internal_trees() -> None:
    assert ALLOWLIST.is_file(), "scripts/export-allowlist.txt does not exist yet"
    rules = [ln.split()[0] for ln in ALLOWLIST.read_text(encoding="utf-8").splitlines()
             if ln.strip() and not ln.lstrip().startswith("#")]
    assert "docs/" not in rules, "list the docs subfolders one by one, so a new one cannot ship unreviewed"
    assert {"docs/static/", "README.md", "LICENSE"} <= set(rules)
    assert "!docs/" in rules, "docs/ is default-deny: only the docs listed after it ship"
    assert {f"!{p}" for p in EXPORT_TOOLING} <= set(rules)
    exporting_internal = [r for r in rules if not r.startswith("!") and INTERNAL_RE.search(r)]
    assert not exporting_internal, "no rule exports internal material"


# ------------------------------------------------------------------------------------------- this repository's HEAD
def _head_paths() -> list[str]:
    raw = lib_proc.run(["git", "-C", str(REPO), "ls-tree", "-r", "-z", "--name-only", "HEAD"], check=True,
                       capture_output=True).stdout.decode()
    return [p for p in raw.split("\0") if p]


@pytest.fixture(scope="module")
def head_export(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, subprocess.CompletedProcess[str]]:
    assert SCRIPT.is_file(), "scripts/export-public.sh does not exist yet"
    dest = tmp_path_factory.mktemp("head-export") / "tree"
    # --allow-dirty: what is under test is HEAD, which is exported whatever the working tree holds.
    proc = lib_proc.run([BASH, str(SCRIPT), "--allow-dirty", str(dest)], cwd=REPO, capture_output=True, text=True,
                        check=False)
    assert proc.returncode == 0, out(proc)
    return dest, proc


def test_head_export_leaves_out_only_the_design_log_and_the_export_tooling(
    head_export: tuple[Path, subprocess.CompletedProcess[str]],
) -> None:
    """docs/ is default-deny: a new docs file or folder fails here until it is exported or kept internal on purpose."""
    dest, _ = head_export
    exported = set(manifest_paths(dest))
    head = set(_head_paths())
    assert exported == files_under(dest)
    unexpected = sorted(head - exported - EXPORT_TOOLING - {p for p in head if p.startswith(SDD)})
    assert not unexpected, f"left out of the export without a decision (scripts/export-allowlist.txt): {unexpected}"
    assert EXPORT_TOOLING <= head - exported
    assert not [p for p in exported if INTERNAL_RE.search(p)], "internal material in the export"
    assert {"README.md", "LICENSE", "docs/static/architecture.png", "docs/static/architecture.drawio"} <= exported


def test_head_export_is_byte_identical_to_the_committed_blobs(
    head_export: tuple[Path, subprocess.CompletedProcess[str]],
) -> None:
    dest, _ = head_export
    paths = manifest_paths(dest)
    listing = lib_proc.run(["git", "-C", str(REPO), "ls-tree", "-r", "-z", "HEAD"], check=True,
                           capture_output=True).stdout.decode()
    committed = {}
    for rec in filter(None, listing.split("\0")):
        meta, rel = rec.split("\t", 1)
        mode, _, blob = meta.split()
        committed[rel] = (mode, blob)
    hashed = lib_proc.run(["git", "-C", str(REPO), "hash-object", "--no-filters", "--stdin-paths"],
                          input="\n".join(str(dest / p) for p in paths) + "\n", check=True, capture_output=True,
                          text=True).stdout.split()
    assert dict(zip(paths, hashed, strict=True)) == {p: committed[p][1] for p in paths}
    modes = {ln.split(" ", 2)[2]: ln.split(" ", 1)[0] for ln in read_manifest(dest)[1]}
    assert modes == {p: committed[p][0] for p in paths}


def test_head_export_passes_the_sanitize_scan(head_export: tuple[Path, subprocess.CompletedProcess[str]]) -> None:
    """The scanner's only allowlisted prefix (the design log) is not exported: every exported file must be clean."""
    dest, _ = head_export
    paths = manifest_paths(dest)
    assert not [p for p in paths if p.startswith(S.ALLOWLIST_PREFIX)]
    # docs/static/architecture.drawio used to be scanned only by test_sanitization_guards: the scanner's e-mail
    # pattern backtracked quadratically on its base64 (~2 min). The look-behind made the scan linear, so the big
    # file is scanned here with every other exported file.
    assert "docs/static/architecture.drawio" in paths
    hits = {}
    for rel in paths:
        data = (dest / rel).read_bytes()
        if b"\0" in data:
            continue
        found = S.scan_text(data.decode("utf-8", errors="ignore"))
        if found:
            hits[rel] = found[:3]
    assert not hits, f"exported files carry deployment-specific identifiers: {hits}"


def test_head_export_passes_its_own_check(head_export: tuple[Path, subprocess.CompletedProcess[str]]) -> None:
    dest, made = head_export
    proc = lib_proc.run([BASH, str(SCRIPT), "--check", str(dest)], cwd=REPO, capture_output=True, text=True,
                        check=False)
    assert proc.returncode == 0, out(proc)
    assert tree_hash(proc) == tree_hash(made) == read_manifest(dest)[0]["tree-sha256"]


def test_head_dry_run_agrees_with_the_export(head_export: tuple[Path, subprocess.CompletedProcess[str]]) -> None:
    dest, _ = head_export
    proc = lib_proc.run([BASH, str(SCRIPT), "--dry-run"], cwd=REPO, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, out(proc)
    exported, excluded = dry_run_sides(proc)
    assert sorted(exported) == sorted(manifest_paths(dest))
    assert sorted(exported + excluded) == sorted(_head_paths())
