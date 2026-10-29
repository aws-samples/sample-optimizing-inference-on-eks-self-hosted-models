# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Every Python package that a documented command adds at run time is pinned to an exact version.

The suites, the tools' tests and ``scripts/preflight-flags.sh`` add their Python packages at run time with the uv
options ``--with`` (one requirement) and ``--with-requirements`` (a requirements file).  An unpinned requirement
resolves to whatever release the index serves that day, so each one names an exact version, and the version of a
package is defined once.

What is checked
===============
* ``tests/requirements-test.txt`` (``PIN_FILE``) carries the MIT-0 SPDX header and pins every package of the test
  environment with ``==`` (an environment marker may follow ``;``): pytest, PyYAML, jsonschema, defusedxml and every
  package they pull in, so that the same set is installed on every run.
* defusedxml, the XML parser of ``tests/publish/lib_xml.py`` and ``tools/demo_bench/tests/lib_xml.py``, has one
  version: the ``dev`` group of ``tools/demo_bench/pyproject.toml`` pins the version of ``PIN_FILE`` and
  ``tools/demo_bench/uv.lock`` resolves it.
* No tracked text file outside the tree scanner's excluded prefix (``sanitize_scan.ALLOWLIST_PREFIX``, history that
  is not exported) passes a requirement to ``--with`` without ``==``.  Nothing is allowlisted.
* Every file passed to ``--with-requirements`` is tracked (read from the repository root, or from the directory of a
  ``cd <dir>`` earlier on the same line) and holds exact pins only.
* A package pinned inline with ``--with`` that is also pinned in a pin file of the repository (``PIN_FILES``) carries
  the version of that file.
* The documented suite commands of tests/README.md, the re-scan checklist of docs/INCIDENT-RESPONSE.md, the
  dashboards and matrix-driver test commands and the matrix-driver ``pyproject.toml`` comment read ``PIN_FILE``.
"""

from __future__ import annotations

import re
import tomllib

import sanitize_scan

ROOT = sanitize_scan.REPO_ROOT
PIN_FILE = "tests/requirements-test.txt"
PIN_FILES = (PIN_FILE, "tools/stage_model/requirements.txt")
TEST_PACKAGES = ("pytest", "pyyaml", "jsonschema", "defusedxml")
SPDX_HEADER = ["# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.",
               "# SPDX-" "License-Identifier: MIT-0"]

# The option and its value, separated by a space or '='.  The look-ahead keeps the longer options apart.
WITH_RE = re.compile(r"--with(?![\w-])(?:=|[ \t]+)([^\s`\\)]+)")
WITH_REQUIREMENTS_RE = re.compile(r"--with-requirements(?:=|[ \t]+)([^\s`\\)]+)")
CD_RE = re.compile(r"\bcd[ \t]+([^\s;&|)`]+)")
READS_PIN_FILE = "--with-requirements" + " " + PIN_FILE
PIN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[\w,.-]+\])?==([A-Za-z0-9][A-Za-z0-9.+!-]*)\s*(?:;.*)?$")

# Files whose documented test commands must read PIN_FILE, with the suites each one runs.
PIN_FILE_COMMANDS = {
    "tests/README.md": ("tests/manifests", "tests/scripts", "tests/terraform", "tests/publish", "tests/dashboards",
                        "tools/matrix_driver/tests"),
    "docs/INCIDENT-RESPONSE.md": ("tests/publish", "tests/scripts/test_152_stage_model_hygiene.py"),
    "tests/dashboards/README.md": ("tests/dashboards",),
    "tools/matrix_driver/README.md": ("tools/matrix_driver/tests",),
    "tools/matrix_driver/pyproject.toml": ("tools/matrix_driver/tests",),
}


def _norm(name: str) -> str:
    """PEP 503 normalised package name."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _line(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _texts() -> dict[str, str]:
    """Tracked text files outside the excluded prefix: ``{path: text}``."""
    out: dict[str, str] = {}
    for rel in sanitize_scan.tracked_files():
        if rel.startswith(sanitize_scan.ALLOWLIST_PREFIX):
            continue
        text = sanitize_scan.read_text(rel)
        if text is not None:
            out[rel] = text
    return out


def _requirements(rel: str) -> list[tuple[int, str]]:
    """``(line number, requirement)`` of every non-blank, non-comment line of a requirements file."""
    lines = (ROOT / rel).read_text(encoding="utf-8").splitlines()
    return [(n, ln.strip()) for n, ln in enumerate(lines, 1) if ln.strip() and not ln.lstrip().startswith("#")]


def _pins(rel: str) -> dict[str, str]:
    """``{normalised name: version}`` of a pin file; every requirement line must be an exact pin."""
    out: dict[str, str] = {}
    for n, req in _requirements(rel):
        m = PIN_RE.match(req)
        assert m, f"{rel}:{n}: {req!r} is not an exact pin (name==version, optional '; marker')"
        name = _norm(m.group(1))
        assert name not in out, f"{rel}:{n}: {name} is pinned twice"
        out[name] = m.group(2)
    return out


def _with_values(text: str) -> list[tuple[int, str]]:
    """``(line, requirement)`` for every requirement passed to ``--with`` (a comma-separated value is split)."""
    out = []
    for m in WITH_RE.finditer(text):
        for req in m.group(1).strip("'\"").split(","):
            out.append((_line(text, m.start()), req.strip("'\"")))
    return out


# ------------------------------------------------------------------------------------------------- the pin file

def test_pin_file_has_the_header_and_pins_every_package_exactly() -> None:
    assert PIN_FILE in set(sanitize_scan.tracked_files()), f"{PIN_FILE} must be a tracked file"
    head = (ROOT / PIN_FILE).read_text(encoding="utf-8").splitlines()[:2]
    assert head == SPDX_HEADER, f"{PIN_FILE}: the first two lines must be the MIT-0 SPDX header, got {head}"
    pins = _pins(PIN_FILE)
    missing = [p for p in TEST_PACKAGES if p not in pins]
    assert not missing, f"{PIN_FILE} must pin {missing}"


def test_defusedxml_has_one_version_in_the_test_pins_and_the_demo_bench_dev_group() -> None:
    version = _pins(PIN_FILE).get("defusedxml")
    assert version, f"{PIN_FILE} must pin defusedxml (the parser of the lib_xml helpers)"
    project = tomllib.loads((ROOT / "tools/demo_bench/pyproject.toml").read_text(encoding="utf-8"))
    dev = [r for r in project["dependency-groups"]["dev"] if _norm(re.split(r"[=<>!~;\[ ]", r, maxsplit=1)[0]) == "defusedxml"]
    assert dev == [f"defusedxml=={version}"], f"the dev group of tools/demo_bench pins defusedxml=={version}: {dev}"
    lock = tomllib.loads((ROOT / "tools/demo_bench/uv.lock").read_text(encoding="utf-8"))
    locked = [p["version"] for p in lock["package"] if p["name"] == "defusedxml"]
    assert locked == [version], f"tools/demo_bench/uv.lock resolves defusedxml {version}: {locked}"


def test_pin_files_hold_exact_pins_only() -> None:
    for rel in PIN_FILES:
        assert (ROOT / rel).is_file(), f"{rel} missing"
        assert _pins(rel), f"{rel} pins nothing"


# ----------------------------------------------------------------------------------------- the tree, every file

def test_no_unpinned_with_requirement_in_the_tree() -> None:
    bad = [f"{rel}:{n}: `--with` {req}" for rel, text in sorted(_texts().items())
           for n, req in _with_values(text) if "==" not in req]
    assert not bad, ("pin every requirement of `--with` to an exact version (name==version), or read "
                     f"{PIN_FILE} with `--with-requirements`:\n" + "\n".join(bad))


def test_with_requirements_files_are_tracked_exact_pin_files() -> None:
    tracked = set(sanitize_scan.tracked_files())
    seen = []
    bad = []
    for rel, text in sorted(_texts().items()):
        for m in WITH_REQUIREMENTS_RE.finditer(text):
            arg = m.group(1).strip("'\"")
            line = text[text.rfind("\n", 0, m.start()) + 1:m.start()]
            bases = [ROOT] + [ROOT / d for d in CD_RE.findall(line)]
            found = [(b / arg).resolve().relative_to(ROOT.resolve()).as_posix() for b in bases
                     if (b / arg).resolve().is_relative_to(ROOT.resolve())]
            hits = [f for f in found if f in tracked]
            seen.append(f"{rel}:{_line(text, m.start())}")
            if not hits:
                bad.append(f"{rel}:{_line(text, m.start())}: `--with-requirements` {arg} is not a tracked file")
                continue
            _pins(hits[0])
    assert seen, f"no `--with-requirements` in the tree: the documented commands read {PIN_FILE}"
    assert not bad, "\n".join(bad)


def test_inline_pins_match_the_pin_files() -> None:
    defined: dict[str, tuple[str, str]] = {}
    for rel in PIN_FILES:
        for name, version in _pins(rel).items():
            defined.setdefault(name, (version, rel))
    bad = []
    for rel, text in sorted(_texts().items()):
        for n, req in _with_values(text):
            m = PIN_RE.match(req)
            if not m or _norm(m.group(1)) not in defined:
                continue
            version, source = defined[_norm(m.group(1))]
            if m.group(2) != version:
                bad.append(f"{rel}:{n}: {req} but {source} pins {version}")
    assert not bad, "an inline pin must carry the version of the pin file:\n" + "\n".join(bad)


# -------------------------------------------------------------------------------------- the documented commands

def test_documented_test_commands_read_the_pin_file() -> None:
    bad = []
    for rel, suites in PIN_FILE_COMMANDS.items():
        lines = (ROOT / rel).read_text(encoding="utf-8").splitlines()
        for suite in suites:
            cmds = [ln for ln in lines if re.search(r"uv run\b.*\bpytest " + re.escape(suite) + r"(?![\w/.-])", ln)]
            if not cmds:
                bad.append(f"{rel}: no 'uv run ... pytest {suite}' command")
            bad += [f"{rel}: {ln.strip()}" for ln in cmds if READS_PIN_FILE not in ln]
    assert not bad, f"these test commands must read {PIN_FILE} with `--with-requirements`:\n" + "\n".join(bad)
