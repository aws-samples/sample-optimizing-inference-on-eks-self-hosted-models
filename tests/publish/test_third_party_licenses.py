# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""THIRD-PARTY-LICENSES.md: the third-party material in the tree and the software the sample uses, with licences.

The file has two parts, each made of Markdown tables with a ``License`` column:

* SHIPPED (H2 ``SHIPPED_H2``): files copied or derived from another project. Columns ``SHIPPED_COLUMNS``.
* USED (H2 ``USED_H2``): images, charts, providers, modules, packages and models that the sample pulls at build,
  deploy or run time and does not redistribute. Columns ``USED_COLUMNS``.

What is checked
===============
* The file exists at the root, says that the repository's own code is MIT-0 (link to LICENSE), links NOTICE and
  docs/static/LICENSES.md (the logos of the diagram are listed there, not here), carries none of the strings the public
  docs must not carry (``test_docs_set.FORBIDDEN``, also applied to NOTICE), and its relative links resolve.  Account ids
  are checked with the tree scanner (``sanitize_scan.scan_text``) instead of the FORBIDDEN digit pattern: both files
  quote full upstream commit hashes, and the scanner does not read a 12-digit run inside a hash as an account id.
* Every row names at least one path in its ``In this repository`` cell and every path it names exists in the tree (a
  tracked file, a directory of tracked files, or a glob over them); so does every backticked repository path of the
  file.  Component names are unique over both parts.
* The ``License`` cell is exactly ``not verified`` or an SPDX expression (ids joined by AND / OR, optional
  parentheses) whose ids are in ``APPROVED``; ``DEPENDENCY_ONLY`` ids are accepted in the USED part only, and
  ``AGPL-3.0-only`` only in the one Grafana row of the USED part, which must say that Grafana is not redistributed.
* A ``not verified`` row claims no licence anywhere in the row.  The ``not verified`` rows are counted and reported
  (printed and raised as a warning), never failed.
* Coverage of the tree: every dashboard that dashboards/README.md does not mark as new, every tracked file under
  dashboards/ outside demo/ (README.md aside), the baked traces and every tracked file with a non-Amazon
  SPDX file-copyright line or a non-MIT-0 SPDX licence id is a SHIPPED row.  NOTICE lists exactly the files of the
  Apache-2.0 SHIPPED rows.
* Coverage of the pins: every provider of the Terraform lock files, every registry module, every Helm chart of
  terraform/cluster (version: the variable default), every package of tools/demo_bench/uv.lock, of
  tools/stage_model/requirements.txt, of tests/requirements-test.txt (the test environment, markers dropped) and of the
  bench image Dockerfile (exact pins, the uv override included), the build backend of tools/demo_bench, the
  kubectl, AWS CLI and base image pins of the Dockerfile and the runtime image of versions.env are USED rows with that
  version, whose ``In this repository`` cell names the file that pins it.

Allowlist (documented, nothing else is exempt)
==============================================
* ``DEPENDENCY_ONLY``: permissive licences of pinned Python dependencies that are not on the base list
  (typing-extensions and defusedxml: PSF-2.0; pillow: MIT-CMU; the bundled code of numpy: 0BSD, Zlib, CC0-1.0;
  matplotlib's own licence, which has no SPDX License List id: LicenseRef-Matplotlib).  Accepted in the USED part only;
  the reviewer confirms them against the approved-licence list.
* ``AGPL-3.0-only``: the Grafana server that the kube-prometheus-stack chart deploys from the upstream image.
"""

from __future__ import annotations

import fnmatch
import re
import tomllib
import warnings
from dataclasses import dataclass
from pathlib import Path

import pytest
import sanitize_scan
import test_docs_set as ds

ROOT = sanitize_scan.REPO_ROOT
THIS = Path(__file__).resolve().relative_to(ROOT).as_posix()
DOC_NAME = "THIRD-PARTY-LICENSES.md"
DOC = ROOT / DOC_NAME
NOTICE = ROOT / "NOTICE"

SHIPPED_H2 = "Third-party material in this repository"
USED_H2 = "Third-party software used but not contained"
WHERE = "In this repository"
SHIPPED_COLUMNS = ["Component", WHERE, "Upstream", "License", "Changes"]
USED_COLUMNS = ["Component", WHERE, "Version", "License", "Notes"]
NOT_VERIFIED = "not verified"

APPROVED = frozenset({"MIT", "MIT-0", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "MPL-2.0", "CC-BY-4.0"})
AGPL = "AGPL-3.0-only"
DEPENDENCY_ONLY = frozenset({"PSF-2.0", "MIT-CMU", "0BSD", "Zlib", "CC0-1.0", "LicenseRef-Matplotlib"})
KNOWN_IDS = APPROVED | DEPENDENCY_ONLY | {AGPL}
LICENCE_CLAIM = re.compile(
    r"(?<![\w-])(?:" + "|".join(re.escape(i) for i in sorted(KNOWN_IDS, key=len, reverse=True)) + r")(?![\w-])"
    r"|(?i:apache license|mit license|bsd license|mozilla public license|gnu (?:affero )?general public license)"
)
ID_RE = r"\(*[A-Za-z0-9][\w.+-]*\)*"
EXPRESSION_RE = re.compile(rf"^{ID_RE}(?: (?:AND|OR) {ID_RE})*$")
SEPARATOR_RE = re.compile(r"^\|(?:\s*:?-{3,}:?\s*\|)+\s*$")
# Built from fragments so this file is not itself a hit of the header scan below.
SPDX_ID_RE = re.compile(r"SPDX-" + r"License-Identifier:\s*([\w.+-]+)")
SPDX_COPYRIGHT_RE = re.compile(r"SPDX-" + r"FileCopyrightText:\s*(.+)")


@dataclass(frozen=True)
class Row:
    """One table row of THIRD-PARTY-LICENSES.md.

    Attributes:
        part: ``"shipped"``, ``"used"`` or ``"other"`` (the H2 section the table sits in).
        cells: Column header -> cell text.
        line: 1-based line number in the file.
    """

    part: str
    cells: dict[str, str]
    line: int

    @property
    def component(self) -> str:
        """Component name: the link text if the cell is a Markdown link, else the cell without backticks."""
        cell = self.cells.get("Component", "")
        m = re.match(r"^\[([^\]]+)\]\([^)]*\)", cell)
        return (m.group(1) if m else cell).replace("`", "").strip()

    @property
    def licence(self) -> str:
        """The ``License`` cell without backticks."""
        return self.cells.get("License", "").replace("`", "").strip()

    @property
    def paths(self) -> list[str]:
        """Backticked tokens of the ``In this repository`` cell."""
        return re.findall(r"`([^`]+)`", self.cells.get(WHERE, ""))

    @property
    def text(self) -> str:
        """Every cell of the row, joined."""
        return " | ".join(self.cells.values())

    def label(self) -> str:
        """``line N (component)`` for messages."""
        return f"line {self.line} ({self.component})"


def _doc_text() -> str:
    assert DOC.is_file(), f"{DOC_NAME} is missing at the repository root"
    return DOC.read_text(encoding="utf-8")


def _split_cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _tables(text: str) -> list[tuple[str, list[str], list[Row]]]:
    """Every Markdown table of ``text``: (H2 part, header, rows)."""
    lines = text.splitlines()
    out: list[tuple[str, list[str], list[Row]]] = []
    part = "other"
    i = 0
    while i < len(lines):
        line = lines[i]
        h2 = re.match(r"^## (.+?)\s*$", line)
        if h2:
            title = h2.group(1)
            part = "shipped" if title == SHIPPED_H2 else "used" if title == USED_H2 else "other"
        if line.startswith("|") and i + 1 < len(lines) and SEPARATOR_RE.match(lines[i + 1]):
            header = _split_cells(line)
            rows: list[Row] = []
            j = i + 2
            while j < len(lines) and lines[j].startswith("|"):
                cells = _split_cells(lines[j])
                assert len(cells) == len(header), f"{DOC_NAME}:{j + 1}: {len(cells)} cells, header has {len(header)}"
                rows.append(Row(part, dict(zip(header, cells, strict=True)), j + 1))
                j += 1
            out.append((part, header, rows))
            i = j
            continue
        i += 1
    return out


def _rows(part: str | None = None) -> list[Row]:
    rows = [r for p, header, rs in _tables(_doc_text()) if "License" in header for r in rs]
    return [r for r in rows if part is None or r.part == part]


def _ids(expression: str) -> list[str]:
    return [t for t in re.split(r"[\s()]+", expression) if t and t not in {"AND", "OR"}]


def _norm(name: str) -> str:
    return re.sub(r"[\s_.-]+", "-", name.strip().lower())


def _version_tokens(cell: str) -> set[str]:
    return set(re.findall(r"[A-Za-z0-9][\w.+-]*[A-Za-z0-9]|[A-Za-z0-9]", cell.replace("`", "")))


def _tracked() -> set[str]:
    return set(sanitize_scan.tracked_files())


def _exists(token: str, tracked: set[str]) -> bool:
    token = token.rstrip("/")
    if any(ch in token for ch in "*?["):
        return any(fnmatch.fnmatch(t, token) for t in tracked)
    return token in tracked or any(t.startswith(token + "/") for t in tracked)


def _shipped_paths() -> set[str]:
    return {p for r in _rows("shipped") for p in r.paths}


# ---------------------------------------------------------------------------------------------------- the file itself

def test_file_exists_and_states_the_repository_licence() -> None:
    text = _doc_text()
    assert re.search(r"^# ", text, re.MULTILINE), "a title"
    assert "MIT-0" in text and re.search(r"\]\(LICENSE\)", text), "the repository's own code is MIT-0: link LICENSE"
    assert re.search(r"\]\(NOTICE\)", text), "link NOTICE (the Apache-2.0 attribution notices)"
    assert re.search(r"\]\(docs/static/LICENSES\.md\)", text), "the diagram logos are covered by docs/static/LICENSES.md"
    for h2 in (SHIPPED_H2, USED_H2):
        assert re.search(rf"^## {re.escape(h2)}\s*$", text, re.MULTILINE), f"H2 '{h2}' missing"


@pytest.mark.parametrize("name", [DOC_NAME, "NOTICE"])
def test_forbidden_strings_absent(name: str) -> None:
    path = ROOT / name
    assert path.is_file(), f"{name} missing"
    text = path.read_text(encoding="utf-8")
    bad = [f"{name}:{text.count(chr(10), 0, m.start()) + 1} {what}: {m.group(0)[:50]!r}"
           for pat, what in ds.FORBIDDEN if what != "account id" for m in re.finditer(pat, text)]
    bad += [f"{name}: {category}: {hit!r}" for category, hit in sanitize_scan.scan_text(text)]
    assert not bad, "\n".join(bad[:12])


def test_relative_links_resolve() -> None:
    bad = []
    for m in re.finditer(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)", _doc_text()):
        target = m.group(1)
        if re.match(r"[a-z]+:", target):
            continue
        path_part, _, anchor = target.partition("#")
        dest = (ROOT / path_part) if path_part else DOC
        if not dest.exists():
            bad.append(f"{target} (no such file)")
        elif anchor and dest.suffix == ".md":
            heads = {ds._slug(h) for h in re.findall(r"^#+ (.+)$", dest.read_text(encoding="utf-8"), re.MULTILINE)}
            if anchor not in heads:
                bad.append(f"{target} (no such heading)")
    assert not bad, f"broken links: {bad}"


def test_tables_sit_in_the_two_parts_with_the_expected_columns() -> None:
    tables = [(p, h, rs) for p, h, rs in _tables(_doc_text()) if "License" in h]
    assert [p for p, _, _ in tables if p == "other"] == [], "every table with a License column belongs to one of the two parts"
    shipped = [h for p, h, _ in tables if p == "shipped"]
    used = [h for p, h, _ in tables if p == "used"]
    assert len(shipped) == 1 and shipped[0] == SHIPPED_COLUMNS, f"one SHIPPED table with columns {SHIPPED_COLUMNS}: {shipped}"
    assert len(used) >= 4 and all(h == USED_COLUMNS for h in used), f"USED tables with columns {USED_COLUMNS}: {used}"
    assert all(rs for _, _, rs in tables), "no empty table"


# -------------------------------------------------------------------------------------------------- rows and licences

def test_every_named_path_exists() -> None:
    tracked = _tracked()
    bad = []
    for r in _rows():
        if not r.paths:
            bad.append(f"{r.label()}: names no path in '{WHERE}'")
        bad += [f"{r.label()}: {p} does not exist" for p in r.paths if not _exists(p, tracked)]
    top = "|".join(ds.TOP_DIRS)
    for tok in re.findall(r"`([^`\s]+)`", _doc_text()):
        if re.match(rf"(?:{top})/", tok) and not _exists(tok, tracked):
            bad.append(f"backticked path {tok} does not exist")
    assert not bad, "\n".join(bad)


def test_component_names_are_unique() -> None:
    names = [_norm(r.component) for r in _rows()]
    dup = sorted({n for n in names if names.count(n) > 1})
    assert not dup, f"one row per component (a 'not verified' row may not have a licensed twin): {dup}"


def test_licence_cells_are_spdx_expressions_or_not_verified() -> None:
    bad = [f"{r.label()}: {r.licence!r}" for r in _rows()
           if r.licence != NOT_VERIFIED and not EXPRESSION_RE.match(r.licence)]
    assert not bad, f"License must be '{NOT_VERIFIED}' or an SPDX expression (ids, AND, OR, parentheses):\n" + "\n".join(bad)


def test_licence_ids_are_on_the_approved_list() -> None:
    bad = []
    for r in _rows():
        if r.licence == NOT_VERIFIED:
            continue
        allowed = APPROVED | (DEPENDENCY_ONLY if r.part == "used" else frozenset())
        for lid in _ids(r.licence):
            if lid == AGPL:
                continue
            if lid not in allowed:
                bad.append(f"{r.label()}: {lid} ({r.part}) is not approved")
    assert not bad, "\n".join(bad)


def test_agpl_only_for_the_grafana_server_which_is_not_redistributed() -> None:
    agpl = [r for r in _rows() if AGPL in _ids(r.licence)]
    assert len(agpl) == 1, f"exactly one {AGPL} row (the Grafana server), got {[r.label() for r in agpl]}"
    row = agpl[0]
    assert row.part == "used" and "Grafana" in row.component, f"{AGPL} is accepted for the deployed Grafana server only"
    assert re.search(r"(?i)not redistributed", row.text), "the Grafana row must say that Grafana is not redistributed"


def test_not_verified_rows_claim_no_licence() -> None:
    bad = [f"{r.label()}: {m.group(0)!r}" for r in _rows() if r.licence == NOT_VERIFIED
           for m in LICENCE_CLAIM.finditer(r.text)]
    assert not bad, f"a '{NOT_VERIFIED}' row must not name a licence:\n" + "\n".join(bad)


def test_not_verified_rows_are_counted_and_reported() -> None:
    rows = [r for r in _rows() if r.licence == NOT_VERIFIED]
    report = f"{len(rows)} '{NOT_VERIFIED}' row(s) in {DOC_NAME}: " + "; ".join(r.label() for r in rows)
    print(report)
    if rows:
        warnings.warn(report, UserWarning, stacklevel=1)


# ---------------------------------------------------------------------------------------------------- tree coverage

def _readme_dashboards() -> list[tuple[str, str]]:
    """(``dashboards/<file>``, origin) for every file of the first table of dashboards/README.md."""
    text = (ROOT / "dashboards/README.md").read_text(encoding="utf-8")
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("|") and "Origin" in _split_cells(line) and i + 1 < len(lines) and SEPARATOR_RE.match(lines[i + 1]):
            header = _split_cells(line)
            out = []
            for row in lines[i + 2:]:
                if not row.startswith("|"):
                    break
                cells = dict(zip(header, _split_cells(row), strict=True))
                out += [(f"dashboards/{f}", cells["Origin"]) for f in re.findall(r"`([^`]+)`", cells[header[0]])]
            return out
    pytest.fail("dashboards/README.md: no table with an Origin column")


def test_vendored_dashboards_of_the_dashboards_readme_are_covered() -> None:
    listed = _readme_dashboards()
    vendored = [f for f, origin in listed if not origin.strip().lower().startswith("new")]
    assert vendored, "dashboards/README.md lists no vendored dashboard"
    missing = [f for f in vendored if f not in _shipped_paths()]
    assert not missing, f"vendored or modified dashboards without a SHIPPED row: {missing}"


def test_every_third_party_dashboard_file_is_covered() -> None:
    files = sorted(t for t in _tracked() if t.startswith("dashboards/") and not t.startswith("dashboards/demo/")
                   and t != "dashboards/README.md")
    missing = [f for f in files if f not in _shipped_paths()]
    assert not missing, f"dashboards outside dashboards/demo (original) need a SHIPPED row: {missing}"


def test_baked_traces_are_covered() -> None:
    traces = sorted(t for t in _tracked() if fnmatch.fnmatch(t, "tools/bench-image/traces/*.jsonl"))
    assert traces, "no baked trace found"
    missing = [t for t in traces if t not in _shipped_paths()]
    assert not missing, f"baked traces without a SHIPPED row: {missing}"


W2_FIXTURE = "tools/demo_bench/tests/fixtures/w2_qwen38/window.jsonl"
W2_PROVENANCE = "tools/demo_bench/tests/README.md"


def test_w2_test_trace_row_points_at_its_recorded_reproduction() -> None:
    """The W2 test trace takes its output lengths from the upstream file of the baked agentic trace: its row names that
    commit, links the recorded command and seed (checked and replayed by
    tools/demo_bench/tests/unit/test_w2_fixture_provenance.py) and carries the licence of that source; the README
    summary no longer calls the derivation inferred."""
    rows = [r for r in _rows("shipped") if W2_FIXTURE in r.paths]
    assert len(rows) == 1, f"one SHIPPED row for {W2_FIXTURE}"
    row = rows[0]
    assert W2_PROVENANCE in row.text, f"{row.label()}: link the recorded command and seed ({W2_PROVENANCE})"
    for stale in ("not recorded", "inferred", "not documented"):
        assert stale not in row.text, f"{row.label()}: stale wording {stale!r}, the command and seed are recorded"
    assert "66a95d5aa3b5d238fd94b6f8243c4e479cb7242c" in row.cells["Upstream"], row.label()
    assert row.licence == "Apache-2.0", f"{row.label()}: derived from the Apache-2.0 upstream trace"
    assert "inferred" not in (ROOT / "README.md").read_text(encoding="utf-8"), "README.md: the derivation is recorded"


def test_files_with_third_party_spdx_headers_are_covered() -> None:
    flagged = []
    for rel in sorted(_tracked()):
        if rel.startswith(sanitize_scan.ALLOWLIST_PREFIX) or rel == THIS:
            continue
        text = sanitize_scan.read_text(rel) or ""
        ids = {m.group(1) for m in SPDX_ID_RE.finditer(text)} - {"MIT-0"}
        holders = [m.group(1) for m in SPDX_COPYRIGHT_RE.finditer(text) if "Amazon.com" not in m.group(1)]
        if ids or holders:
            flagged.append(rel)
    assert flagged, "the vendored Dynamo dashboards carry an Apache-2.0 SPDX header: the scan found nothing"
    missing = [f for f in flagged if f not in _shipped_paths()]
    assert not missing, f"files with a third-party SPDX header need a SHIPPED row: {missing}"


def _notice_paths() -> set[str]:
    """Repository paths that NOTICE lists as items: a line holding only the path, optionally numbered."""
    text = NOTICE.read_text(encoding="utf-8")
    return set(re.findall(r"^\s*(?:\d+\.\s+)?([\w-]+/[\w./-]+)\s*$", text, re.MULTILINE))


def test_notice_lists_exactly_the_apache_2_0_shipped_files() -> None:
    apache = {p for r in _rows("shipped") if "Apache-2.0" in _ids(r.licence) for p in r.paths}
    notice = _notice_paths()
    tracked = _tracked()
    assert notice <= tracked, f"NOTICE lists paths that are not tracked: {sorted(notice - tracked)}"
    assert notice == apache, (f"NOTICE only: {sorted(notice - apache)}; Apache-2.0 SHIPPED rows missing from NOTICE: "
                              f"{sorted(apache - notice)}")


# ---------------------------------------------------------------------------------------------------- pin coverage

def _matches(name: str, version: str, source: str) -> bool:
    return any(_norm(r.component) == _norm(name) and version in _version_tokens(r.cells.get("Version", ""))
               and source in r.paths for r in _rows("used"))


def _assert_listed(pins: list[tuple[str, str, str]], what: str) -> None:
    assert pins, f"no {what} found"
    missing = [f"{n} {v} (pinned in {s})" for n, v, s in pins if not _matches(n, v, s)]
    assert not missing, (f"{what} without a USED row (Component = name, Version holds the version, "
                         f"'{WHERE}' names the pinning file): {missing}")


def test_terraform_providers_of_the_lock_files_are_listed() -> None:
    pins = []
    for lock in sorted(t for t in _tracked() if t.endswith(".terraform.lock.hcl")):
        text = (ROOT / lock).read_text(encoding="utf-8")
        pins += [(m.group(1), m.group(2), lock)
                 for m in re.finditer(r'provider "registry\.terraform\.io/([\w-]+/[\w-]+)" \{\s*version\s*=\s*"([^"]+)"', text)]
    _assert_listed(pins, "Terraform providers")


def test_terraform_registry_modules_are_listed() -> None:
    pins = []
    for tf in sorted(t for t in _tracked() if t.startswith("terraform/") and t.endswith(".tf") and "/tests/" not in t):
        text = (ROOT / tf).read_text(encoding="utf-8")
        for block in re.findall(r'^module "[\w-]+" \{(.*?)^\}', text, re.DOTALL | re.MULTILINE):
            source = re.search(r'^\s*source\s*=\s*"([^"]+)"', block, re.MULTILINE)
            assert source, f"{tf}: module without a source"
            if source.group(1).startswith("."):
                continue
            version = re.search(r'^\s*version\s*=\s*"([^"]+)"', block, re.MULTILINE)
            assert version, f"{tf}: registry module {source.group(1)} is not pinned"
            pins.append((source.group(1), version.group(1), tf))
    _assert_listed(pins, "Terraform registry modules")


def _variable_default(name: str) -> str:
    text = (ROOT / "terraform/cluster/variables.tf").read_text(encoding="utf-8")
    m = re.search(rf'variable "{name}" \{{.*?default\s*=\s*"([^"]+)"', text, re.DOTALL)
    assert m, f"terraform/cluster/variables.tf: no default for {name}"
    return m.group(1)


def test_helm_charts_are_listed() -> None:
    pins = []
    for tf in sorted(t for t in _tracked() if t.startswith("terraform/cluster/") and t.endswith(".tf") and "/tests/" not in t):
        text = (ROOT / tf).read_text(encoding="utf-8")
        for block in re.findall(r'^resource "helm_release" "[\w-]+" \{(.*?)^\}', text, re.DOTALL | re.MULTILINE):
            chart = re.search(r'^\s*chart\s*=\s*"([^"]+)"', block, re.MULTILINE)
            assert chart, f"{tf}: helm_release without a chart"
            url = re.match(r".*/([\w-]+?)-\$\{var\.(\w+)\}\.tgz$", chart.group(1))
            if url:
                pins.append((url.group(1), _variable_default(url.group(2)), tf))
                continue
            version = re.search(r'^\s*version\s*=\s*(?:var\.(\w+)|"([^"]+)")', block, re.MULTILINE)
            assert version, f"{tf}: helm_release {chart.group(1)} has no version"
            pins.append((chart.group(1), _variable_default(version.group(1)) if version.group(1) else version.group(2), tf))
    _assert_listed(pins, "Helm charts")


def test_python_packages_of_the_demo_bench_lock_are_listed() -> None:
    lock = "tools/demo_bench/uv.lock"
    data = tomllib.loads((ROOT / lock).read_text(encoding="utf-8"))
    pins = [(p["name"], p["version"], lock) for p in data["package"] if "registry" in p.get("source", {})]
    _assert_listed(pins, f"packages of {lock}")


def test_build_backend_of_demo_bench_is_listed() -> None:
    src = "tools/demo_bench/pyproject.toml"
    reqs = tomllib.loads((ROOT / src).read_text(encoding="utf-8"))["build-system"]["requires"]
    _assert_listed([(*r.split("=="), src) for r in reqs], f"build requirements of {src}")


def test_stage_model_requirements_are_listed() -> None:
    src = "tools/stage_model/requirements.txt"
    pins = [(*ln.strip().split("=="), src) for ln in (ROOT / src).read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")]
    _assert_listed(pins, f"pins of {src}")


def test_test_requirements_are_listed() -> None:
    src = "tests/requirements-test.txt"
    assert (ROOT / src).is_file(), f"{src} missing"
    pins = [(*ln.split(";")[0].strip().split("=="), src) for ln in (ROOT / src).read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")]
    _assert_listed(pins, f"pins of {src}")


def test_bench_image_pins_are_listed() -> None:
    src = "tools/bench-image/Dockerfile"
    text = (ROOT / src).read_text(encoding="utf-8")
    pins = [(m.group(1), m.group(2), src) for m in re.finditer(r"""["']([A-Za-z0-9][\w.-]*)==([\w.]+)["']""", text)]
    pins += [(m.group(1), m.group(2), src) for m in re.finditer(r"""["']([A-Za-z0-9][\w.-]*)>=([\w.]+)["']""", text)]
    for arg, name in (("KUBECTL_VERSION", "kubectl"), ("AWSCLI_VERSION", "AWS CLI")):
        m = re.search(rf"^ARG {arg}=(\S+)$", text, re.MULTILINE)
        assert m, f"{src}: ARG {arg}"
        pins.append((name, m.group(1), src))
    _assert_listed(pins, f"pins of {src}")
    base = re.search(r"^ARG BASE_IMAGE=([^@\s]+)@sha256:[0-9a-f]{64}$", text, re.MULTILINE)
    assert base, f"{src}: BASE_IMAGE pinned by digest"
    assert any(base.group(1) in r.cells.get("Version", "") and src in r.paths for r in _rows("used")), (
        f"the base image {base.group(1)} needs a USED row naming {src}")


def test_runtime_image_of_versions_env_is_listed() -> None:
    text = (ROOT / "versions.env").read_text(encoding="utf-8")
    m = re.search(r"^RUNTIME_IMAGE=([^@\s]+)@sha256:[0-9a-f]{64}$", text, re.MULTILINE)
    assert m, "versions.env: RUNTIME_IMAGE pinned by digest"
    assert any(m.group(1) in r.cells.get("Version", "") and "versions.env" in r.paths for r in _rows("used")), (
        f"the runtime image {m.group(1)} needs a USED row naming versions.env")
