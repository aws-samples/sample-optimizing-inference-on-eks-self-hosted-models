# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``workloads/w3-16k/METHODOLOGY.md``: the benchmark method and its adherence to the NVIDIA Dynamo benchmarking rules.

Contract
========
* The file exists, has the four H2 sections (what it is and is not, adherence, how to run with your own ISL/OSL/rate,
  limits) and ``workloads/w3-16k/README.md`` links it.
* Adherence table (``| Rule | Requirement | Status | Evidence or reason |``): every rule file of
  ``agent-docs/rules/benchmarking`` of the Dynamo repository (``RULE_FILES``, read at ``UPSTREAM_COMMIT``) has at least
  one row and no row names an unknown rule; the other rows name one of the AIPerf skills or the AIPerf CLI guide.  Each
  status is ``followed``, ``deviates`` or ``not applicable``; each evidence cell is filled; a ``followed`` row cites a
  repository file and every file it cites exists.  The deviations the method must own (one run per cell and rate, the
  own synthetic workload, the single node, the derived SGLang build, the open-loop schedule) each sit in a ``deviates``
  row.  With a Dynamo checkout (``DYNAMO_REPO``, default a sibling directory named ``dynamo``) that has
  ``UPSTREAM_COMMIT``, ``RULE_FILES`` must equal the rule files of that commit; without one the comparison skips.
* Every repository path the document names exists and every relative link resolves.
* The worked example runs offline exactly as written (``uv run --project tools/demo_bench demo-bench trace w2``; only its
  output directory is replaced): the generator honours its ``--expect-sha256``, the largest ``input_length`` is
  ``entry + (turns - 1) x growth`` and fits ``--max-isl``, the stated window holds no entry and ends before the first
  sessions end, the cut keeps the stated number of rows and the stated cold TTFT limit follows from the SLO line.
* The new files pass the public-tree guards: vocabulary, process wording, original region, the deny list of the main
  docs, the tree scanner and the W3 folder rules (no bucket URI, region literal or 12-digit number).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest
import sanitize_scan
import test_docs_set as ds
import test_sub_cycle_f_guards as f_guards

ROOT = sanitize_scan.REPO_ROOT
W3 = ROOT / "workloads" / "w3-16k"
METHOD = W3 / "METHODOLOGY.md"
W3_README = W3 / "README.md"
THIS = Path(__file__).resolve()
LINK_LINE = "- Method and adherence to the NVIDIA Dynamo benchmarking rules: [METHODOLOGY.md](METHODOLOGY.md)"

# The rule files of agent-docs/rules/benchmarking in the Dynamo repository at UPSTREAM_COMMIT (main, 2026-10-17).
UPSTREAM_COMMIT = "934d09aaff2a4a4e7c2049ebfb8197a9d930e3b2"
RULE_FILES = frozenset({
    "benchmark-isolation.md", "comparison-uncertainty.md", "concurrency-grid.md", "evidence-eligibility.md",
    "proxy-workload-selection.md", "result-storage.md", "series-boundaries.md", "tool-version.md",
})
OTHER_SOURCES = frozenset({
    "configure-aiperf-benchmark", "run-aiperf-benchmark", "analyze-aiperf-results", "benchmarking-with-aiperf.mdx",
})
STATUSES = frozenset({"followed", "deviates", "not applicable"})
TABLE_HEADER = ("Rule", "Requirement", "Status", "Evidence or reason")
OWNED_DEVIATIONS = {
    "one run per cell and rate": r"n=1",
    "own synthetic workload instead of a recipe proxy": r"demo-bench trace w2",
    "single node of 8 GPUs": r"(?i)\b(?:one|single)[ -]node\b",
    "derived SGLang 0.5.22 build vs the shipped 0.5.21 image": r"0\.5\.22",
    "open-loop schedule instead of a concurrency grid": r"(?i)open[- ]loop",
}
EXAMPLE_DIR = "/tmp/my-trace"  # the output directory of the worked example; the test replaces it with tmp_path
W2_KEYS = ("--entry-tokens", "--growth-tokens", "--turns", "--global-prefix-tokens", "--max-isl", "--cold-fraction",
           "--cold-min-tokens", "--cold-max-tokens", "--osl", "--osl-file", "--osl-cap", "--rate", "--sessions",
           "--seed", "--expect-sha256")
SLO_KEYS = ("ttft_s", "itl_ms", "cold_factor", "cold_prefill_tok_s")
# The W3 folder rules of tests/scripts/test_108 (that suite reads every file of workloads/w3-16k).
W3_FOLDER_RULES = {
    "12-digit number": re.compile(r"(?<![A-Za-z0-9])[0-9]{12}(?![A-Za-z0-9])"),
    "region literal": re.compile(
        r"(?<![A-Za-z0-9])(?:us|eu|ap|sa|ca|me|af|il|mx)-(?:north|south|east|west|central|northeast|southeast|northwest"
        r"|southwest)-[0-9](?![A-Za-z0-9])"),
    "short region code": re.compile(r"(?<![A-Za-z0-9])(?:use|usw|euw|euc|eun|aps|apn|sae|cac)[0-9](?![A-Za-z0-9])"),
    "bucket URI": re.compile(re.escape("s3" + "://")),
}


# ----------------------------------------------------------------------------------------------------------- helpers
def _text() -> str:
    assert METHOD.is_file(), f"missing {METHOD.relative_to(ROOT)}"
    return METHOD.read_text(encoding="utf-8")


def _section(text: str, heading_pattern: str, level: int = 2) -> str:
    """Body of the first heading of ``level`` whose title matches ``heading_pattern`` (up to the next heading <= level)."""
    m = re.search(rf"(?m)^{'#' * level} .*(?:{heading_pattern}).*$", text)
    assert m, f"METHODOLOGY.md has no {'#' * level} heading matching {heading_pattern!r}"
    rest = text[m.end():]
    end = re.search(rf"(?m)^#{{1,{level}}} ", rest)
    return rest[: end.start()] if end else rest


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _adherence_rows() -> list[dict[str, str]]:
    body = _section(_text(), r"Adherence")
    lines = [ln for ln in body.splitlines() if ln.lstrip().startswith("|")]
    assert len(lines) > 2, "the adherence section has no table"
    assert tuple(_cells(lines[0])) == TABLE_HEADER, f"adherence table header must be {TABLE_HEADER}: {_cells(lines[0])}"
    assert re.fullmatch(r"\|(?:\s*:?-{3,}:?\s*\|)+", lines[1].strip()), "second line must be the separator row"
    rows = []
    for ln in lines[2:]:
        cells = _cells(ln)
        assert len(cells) == len(TABLE_HEADER), f"row with {len(cells)} cells: {ln}"
        rows.append(dict(zip(TABLE_HEADER, cells, strict=True)))
    return rows


def _rule_names(cell: str) -> set[str]:
    return set(re.findall(r"`([^`]+)`", cell))


def _cited_paths(text: str) -> tuple[set[str], set[str]]:
    """(repository paths named in ``text``, relative link targets of ``text``), placeholders dropped."""
    plain = re.sub(r"[\w./-]*[<{$][^\s`)]*", "", text)
    paths = set(ds._clean_candidates(plain))
    links = {m.group(1).split("#", 1)[0] for m in re.finditer(r"\]\(([^)\s]+)\)", text)}
    links = {t for t in links if t and not re.match(r"[a-z]+:", t)}
    return paths, links


def _exists(token: str) -> bool:
    for base in (ROOT, W3):
        if "*" in token:
            if list(base.glob(token)):
                return True
        elif (base / token).exists():
            return True
    return False


def _bash_blocks(section: str) -> list[str]:
    return re.findall(r"(?ms)^```bash\n(.*?)^```", section)


def _example() -> tuple[str, str, str]:
    """(worked example section, generator block, cut block)."""
    how = _section(_text(), r"How to run with your own")
    ex = _section(how, r"Worked example", level=3)
    blocks = _bash_blocks(ex)
    gen = [b for b in blocks if "demo-bench trace w2" in b]
    cut = [b for b in blocks if "head -n" in b]
    assert len(gen) == 1 and len(cut) == 1, "the worked example has one generator block and one cut block (bash)"
    return ex, gen[0], cut[0]


def _w2_flags(block: str) -> dict[str, str]:
    line = next(ln for ln in block.replace("\\\n", " ").splitlines() if "demo-bench trace w2" in ln)
    argv = shlex.split(line)
    head = ["uv", "run", "--project", "tools/demo_bench", "demo-bench", "trace", "w2"]
    assert argv[: len(head)] == head, f"the example must run `{' '.join(head)}` from the repository root: {argv[:7]}"
    args = argv[len(head):]
    assert len(args) % 2 == 0 and all(a.startswith("--") for a in args[::2]), f"--flag value pairs only: {args}"
    return dict(zip(args[::2], args[1::2], strict=True))


def _clean_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_") and k not in ("VIRTUAL_ENV", "DYNAMO_REPO")}
    env["LC_ALL"] = "C"
    return env


def _run_bash(block: str, out_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", "-euo", "pipefail", "-c", block.replace(EXAMPLE_DIR, str(out_dir))],
                          cwd=ROOT, env=_clean_env(), capture_output=True, text=True, timeout=900, check=False)


def _int(text: str) -> int:
    return int(text.replace(",", ""))


@pytest.fixture(scope="module")
def example_run(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    """Run the generator and the cut blocks of the worked example once, in a fresh directory."""
    if shutil.which("uv") is None:
        pytest.skip("uv not installed (the worked example runs `uv run --project tools/demo_bench`)")
    ex, gen, cut = _example()
    assert EXAMPLE_DIR in gen and EXAMPLE_DIR in cut, f"both blocks write under {EXAMPLE_DIR}"
    out = tmp_path_factory.mktemp("worked-example")
    g = _run_bash(gen, out)
    assert g.returncode == 0, f"generator block failed ({g.returncode}):\n{g.stdout[-2000:]}\n{g.stderr[-2000:]}"
    c = _run_bash(cut, out)
    assert c.returncode == 0, f"cut block failed ({c.returncode}):\n{c.stdout[-2000:]}\n{c.stderr[-2000:]}"
    flags = _w2_flags(gen)
    trace = out / Path(flags["--out"]).relative_to(EXAMPLE_DIR)
    side = out / Path(flags["--sidecar"]).relative_to(EXAMPLE_DIR)
    rows = [json.loads(ln) for ln in trace.read_text().splitlines()]
    sidecar = [json.loads(ln) for ln in side.read_text().splitlines()]
    windows = sorted(p for p in out.iterdir() if p.name.endswith(".jsonl") and p not in (trace, side))
    return {"section": ex, "gen": gen, "cut": cut, "flags": flags, "rows": rows, "sidecar": sidecar, "out": out,
            "window_files": windows, "cut_stdout": c.stdout}


# ------------------------------------------------------------------------------------------------- file and link
def test_methodology_exists_with_its_four_sections() -> None:
    text = _text()
    assert text.startswith("# "), "one H1 title on the first line"
    for pattern in (r"is and is not", r"Adherence", r"How to run with your own", r"Limits"):
        assert _section(text, pattern).strip(), f"empty section {pattern!r}"


def test_w3_readme_links_the_methodology() -> None:
    text = W3_README.read_text(encoding="utf-8")
    assert LINK_LINE in text.splitlines(), f"workloads/w3-16k/README.md must carry the line: {LINK_LINE}"


def test_what_it_is_names_the_tools_and_the_design() -> None:
    body = _section(_text(), r"is and is not")
    for needle in ("AIPerf 0.13.0", "--fixed-schedule", "mooncake_trace", "demo-bench trace w2", "S0", "S7", "in-cluster",
                   "tools/matrix_driver/matrix_driver.py", UPSTREAM_COMMIT[:7]):
        assert needle in body, f"'What this benchmark is and is not' must name {needle!r}"


# ------------------------------------------------------------------------------------------------ adherence table
def test_adherence_table_covers_every_rule_file_and_nothing_else() -> None:
    rows = _adherence_rows()
    named: set[str] = set()
    for row in rows:
        names = _rule_names(row["Rule"])
        assert len(names) == 1, f"one rule or source per row: {row['Rule']!r}"
        unknown = names - RULE_FILES - OTHER_SOURCES
        assert not unknown, f"unknown rule or source {unknown} (rule files: {sorted(RULE_FILES)})"
        named |= names
    missing = RULE_FILES - named
    assert not missing, f"no adherence row for {sorted(missing)}"


def test_adherence_rows_have_a_status_and_evidence() -> None:
    for row in _adherence_rows():
        assert row["Status"] in STATUSES, f"status must be one of {sorted(STATUSES)}: {row}"
        assert row["Requirement"], f"empty requirement: {row}"
        assert len(row["Evidence or reason"]) >= 20, f"evidence or reason missing: {row}"


def test_followed_rows_cite_an_existing_repository_file() -> None:
    bad = []
    for row in _adherence_rows():
        if row["Status"] != "followed":
            continue
        paths, links = _cited_paths(row["Evidence or reason"])
        if not paths and not links:
            bad.append(f"no file cited: {row['Rule']} / {row['Requirement']}")
        bad += [f"{row['Rule']}: {p} does not exist" for p in sorted(paths | links) if not _exists(p)]
    assert not bad, "\n".join(bad)


@pytest.mark.parametrize("what", sorted(OWNED_DEVIATIONS))
def test_owned_deviation_is_a_deviates_row(what: str) -> None:
    pattern = OWNED_DEVIATIONS[what]
    hits = [r for r in _adherence_rows() if r["Status"] == "deviates"
            and re.search(pattern, r["Requirement"] + " " + r["Evidence or reason"])]
    assert hits, f"the adherence table must state the deviation '{what}' in a 'deviates' row (pattern {pattern!r})"


def test_proxy_row_records_the_trace_source_and_sha256() -> None:
    rows = [r for r in _adherence_rows() if "proxy-workload-selection.md" in r["Rule"]]
    text = " ".join(r["Evidence or reason"] for r in rows)
    assert "workloads/w3-16k/params.json" in text and "sha256" in text, rows


def test_rule_files_match_the_upstream_checkout() -> None:
    repo = Path(os.environ.get("DYNAMO_REPO") or ROOT.parent / "dynamo")
    if not (repo / ".git").exists():
        pytest.skip("needs a Dynamo checkout: set DYNAMO_REPO (default: a sibling directory named 'dynamo')")
    if subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{UPSTREAM_COMMIT}^{{commit}}"],
                      capture_output=True, check=False).returncode != 0:
        pytest.skip(f"the Dynamo checkout at {repo} lacks commit {UPSTREAM_COMMIT[:7]}: fetch origin/main")
    out = subprocess.run(["git", "-C", str(repo), "ls-tree", "--name-only", UPSTREAM_COMMIT, "agent-docs/rules/benchmarking/"],
                         capture_output=True, text=True, check=True).stdout
    upstream = {Path(p).name for p in out.split() if p.endswith(".md")}
    assert upstream == RULE_FILES, f"rule files at {UPSTREAM_COMMIT[:7]}: {sorted(upstream)} (table pins {sorted(RULE_FILES)})"
    skills = subprocess.run(["git", "-C", str(repo), "ls-tree", "--name-only", UPSTREAM_COMMIT, ".agents/skills/"],
                            capture_output=True, text=True, check=True).stdout
    for skill in OTHER_SOURCES - {"benchmarking-with-aiperf.mdx"}:
        assert f".agents/skills/{skill}" in skills.split(), f"{skill} is not a skill at {UPSTREAM_COMMIT[:7]}"


# --------------------------------------------------------------------------------------------- paths and links
def test_every_path_and_link_the_methodology_names_exists() -> None:
    paths, links = _cited_paths(_text())
    bad = sorted(t for t in paths | links if t not in ds.GENERATED and not _exists(t))
    names = {m.group(1) for m in re.finditer(r"`([\w.-]+\.(?:sh|py))(?:\s[^`]*)?`", _text())}
    have = {p.name for p in ROOT.rglob("*.*") if p.is_file() and ".git" not in p.parts and ".venv" not in p.parts}
    bad += sorted(n for n in names if n not in have)
    assert not bad, f"METHODOLOGY.md names paths that do not exist: {bad}"


# ------------------------------------------------------------------------------------------- how to run your own
def _generator_flags() -> set[str]:
    """The flags of `demo-bench trace w2`, read from its argparse definition."""
    source = (ROOT / "tools/demo_bench/src/demo_bench/cli.py").read_text()
    w2 = source[source.index("def _add_trace_w2"):source.index("def _add_w2_gate")]
    return set(re.findall(r'add_argument\(\s*"(--[a-z0-9-]+)"', w2))


def test_how_to_run_documents_the_generator_flags_and_the_slo_line() -> None:
    how = _section(_text(), r"How to run with your own")
    table = [_cells(ln) for ln in how.splitlines() if re.match(r"\|\s*`--", ln)]
    assert table, "'How to run' has a table whose first column lists generator flags"
    in_table = {f for cells in table for f in re.findall(r"`(--[a-z0-9-]+)`", cells[0])}
    real = _generator_flags()
    assert in_table <= real, f"the flag table names flags that `demo-bench trace w2` does not have: {in_table - real}"
    assert set(W2_KEYS) <= in_table, f"the flag table must document {sorted(set(W2_KEYS) - in_table)}"
    for key in SLO_KEYS:
        assert key in how, f"'How to run' must document the plan SLO key {key}"


def test_worked_example_flags_fit_by_construction(example_run: dict[str, object]) -> None:
    flags = example_run["flags"]
    assert isinstance(flags, dict)
    assert "--expect-sha256" in flags, "the example pins its trace with --expect-sha256"
    entry, growth, turns, max_isl = (int(flags[k]) for k in ("--entry-tokens", "--growth-tokens", "--turns", "--max-isl"))
    last = entry + (turns - 1) * growth
    assert last <= max_isl, f"last turn {last} > --max-isl {max_isl}"
    assert int(flags["--cold-max-tokens"]) <= max_isl
    section = str(example_run["section"])
    assert f"{last:,}" in section and f"{max_isl:,}" in section, "the example states its last-turn ISL and --max-isl"


def test_worked_example_trace_fits_max_isl(example_run: dict[str, object]) -> None:
    flags, rows, sidecar = example_run["flags"], example_run["rows"], example_run["sidecar"]
    assert isinstance(flags, dict) and isinstance(rows, list) and isinstance(sidecar, list)
    assert rows and len(rows) == len(sidecar)
    max_isl = int(flags["--max-isl"])
    assert max(r["input_length"] for r in rows) <= max_isl
    last = int(flags["--entry-tokens"]) + (int(flags["--turns"]) - 1) * int(flags["--growth-tokens"])
    assert max(s["input_length"] for s in sidecar if s["class"] != "cold_ingestion") == last
    assert {r["output_length"] for r in rows} == {int(flags["--osl"])}


def test_worked_example_window_and_cut_agree_with_the_text(example_run: dict[str, object]) -> None:
    flags, rows, sidecar = example_run["flags"], example_run["rows"], example_run["sidecar"]
    assert isinstance(flags, dict) and isinstance(rows, list) and isinstance(sidecar, list)
    section, cut = str(example_run["section"]), str(example_run["cut"])
    gap = int(flags["--sessions"]) / float(flags["--rate"])
    m = re.search(r"\[([\d,]+), ([\d,]+)\) s", section)
    assert m, "the example states its measured window as [lo, hi) s"
    lo, hi = _int(m.group(1)), _int(m.group(2))
    assert lo == float(flags["--populate-s"]) + gap, "the window starts one mean gap after the entry window"
    assert hi <= (int(flags["--turns"]) - 1) * gap, "the window ends before the earliest sessions end"
    cut_ms = re.search(r"cut=(\d+)", cut)
    assert cut_ms and int(cut_ms.group(1)) == hi * 1000, "the cut is the end of the window, in ms"
    assert not [s for r, s in zip(rows, sidecar, strict=True) if lo * 1000 <= r["timestamp"] < hi * 1000
                and s["class"] == "entry"], "no entry inside the window"
    k = sum(1 for r in rows if r["timestamp"] < hi * 1000)
    stated = re.search(r"keeps ([\d,]+) rows", section)
    assert stated and _int(stated.group(1)) == k, f"the cut keeps {k} rows"
    windows = example_run["window_files"]
    assert isinstance(windows, list) and len(windows) == 2
    for p in windows:
        assert len(p.read_text().splitlines()) == k, p.name


def test_worked_example_slo_line_and_cold_limit(example_run: dict[str, object]) -> None:
    flags = example_run["flags"]
    assert isinstance(flags, dict)
    section = str(example_run["section"])
    m = re.search(r"slo ttft_s=([\d.]+) itl_ms=([\d.]+) cold_factor=([\d.]+) cold_prefill_tok_s=(\d+)", section)
    assert m, "the example gives a plan SLO line with the four keys"
    limit = float(m.group(3)) * int(flags["--cold-max-tokens"]) / int(m.group(4))
    assert f"{limit:.1f} s" in section, f"the example states the cold TTFT limit of its largest cold row ({limit:.1f} s)"


# ---------------------------------------------------------------------------------------------------- guards
NEW_FILES = (METHOD, THIS)


@pytest.mark.parametrize("path", NEW_FILES, ids=lambda p: p.name)
def test_new_files_pass_the_vocabulary_wording_and_region_guards(path: Path) -> None:
    rel = path.relative_to(ROOT).as_posix()
    text = path.read_text(encoding="utf-8")
    hits = f_guards._vocabulary_hits(rel, text)
    hits += [f"{what}: {m}" for what, pat in f_guards.PROCESS_WORDING.items() for m in re.findall(pat, text)]
    hits += [f"original region: {m}" for m in re.findall(f_guards.ORIGINAL_REGION, text)]
    hits += [f"{kind}: {m}" for kind, m in sanitize_scan.scan_text(text)]
    assert not hits, f"{rel}: {hits[:5]}"


def test_methodology_passes_the_main_docs_deny_list_and_the_w3_folder_rules() -> None:
    text = _text()
    hits = [f"{what}: {m.group(0)!r}" for pat, what in ds.FORBIDDEN for m in re.finditer(pat, text)]
    hits += [f"{what}: {m.group(0)!r}" for what, rx in W3_FOLDER_RULES.items() for m in rx.finditer(text)]
    assert not hits, hits[:5]
