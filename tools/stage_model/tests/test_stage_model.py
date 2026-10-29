# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""RED tests of ``tools/stage_model/stage_model.py``: Hugging Face -> S3 model staging (see conftest.py for the contract)."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from types import ModuleType

import pytest
from conftest import MODULE_PATH
from fakes import (
    BUCKET,
    CONTENT,
    MODEL,
    PREFIX,
    REVISION,
    TOKEN,
    URI,
    WANTED,
    FakeHf,
    FakeS3,
    gated_error,
    marker_fields,
    not_found_error,
    tree_entry,
    unauthorized_error,
)

TOOL = Path(__file__).resolve().parents[1]
EMPTY = ""  # HF_TOKEN set but empty (the optional secretKeyRef can yield it), which the tool treats as unset


def env(**over: str | None) -> dict[str, str]:
    """Return the Job environment; a ``None`` override removes the variable."""
    base = {"MODEL_ID": MODEL, "MODEL_REVISION": REVISION, "MODEL_S3_URI": URI, "AWS_REGION": "us-east-2"}
    base.update({k: v for k, v in over.items() if v is not None})
    for k, v in over.items():
        if v is None:
            base.pop(k, None)
    return base


class Harness:
    """Runs ``main`` with the fakes and collects everything it printed or logged (the token must be in none of it)."""

    def __init__(self, sm: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture) -> None:
        self.sm, self.tmp, self.capsys, self.caplog = sm, tmp_path, capsys, caplog
        self.work = tmp_path / "work"
        self.work.mkdir()

    def run(self, hf: FakeHf, s3: FakeS3, **over: str | None) -> tuple[int, str]:
        self.caplog.set_level(logging.DEBUG)
        rc = self.sm.main([], environ=env(**over), hf=hf, s3=s3, work_dir=self.work)
        cap = self.capsys.readouterr()
        return rc, cap.out + cap.err + self.caplog.text

    def leftovers(self) -> list[Path]:
        return [p for p in self.work.rglob("*") if p.is_file()]


@pytest.fixture
def h(sm: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture) -> Harness:
    return Harness(sm, tmp_path, capsys, caplog)


def staged(s3: FakeS3) -> set[str]:
    """Return the file paths (relative to the prefix) written to S3, without the marker."""
    return {k[len(PREFIX) + 1 :] for k in s3.keys() if k.startswith(PREFIX + "/") and not k.endswith("/.done")}


# ----------------------------------------------------------------------------------- exit-code contract
def test_exit_codes_are_distinct_and_only_ok_is_zero(sm: ModuleType) -> None:
    names = ["EXIT_OK", "EXIT_CONFIG", "EXIT_GATED_NO_TOKEN", "EXIT_ACCESS_DENIED", "EXIT_NOT_FOUND", "EXIT_VERIFY", "EXIT_TRANSFER"]
    codes = [getattr(sm, n) for n in names]
    assert len(set(codes)) == len(codes)
    assert sm.EXIT_OK == 0 and all(c > 0 for c in codes[1:])


# ----------------------------------------------------------------------------------- public model, no token
def test_public_model_without_token_stages_everything_and_writes_marker_last(h: Harness, sm: ModuleType) -> None:
    hf, s3 = FakeHf(gated=False), FakeS3()
    rc, out = h.run(hf, s3)
    assert rc == sm.EXIT_OK, out
    assert staged(s3) == WANTED
    for path in WANTED:
        assert s3.objects[f"{PREFIX}/{path}"] == CONTENT[path]
    marker = marker_fields(s3.objects[f"{PREFIX}/.done"])
    assert MODEL in marker.values() and REVISION in marker.values()
    assert str(len(WANTED)) in marker.values(), f"the marker must carry the file count: {marker}"
    assert s3.write_ops()[-1] == ("put", f"{PREFIX}/.done") or s3.write_ops()[-1] == ("upload", f"{PREFIX}/.done")
    assert all(call_kw["token"] in (None, False, "") for _, call_kw in hf.calls), "a public model needs no token"


def test_public_model_ignores_a_token_that_is_set(h: Harness, sm: ModuleType) -> None:
    """Decision: a token is sent only to a gated model; a public one never sees it (an expired token must not break it)."""
    hf, s3 = FakeHf(gated=False), FakeS3()
    rc, out = h.run(hf, s3, HF_TOKEN=TOKEN)
    assert rc == sm.EXIT_OK, out
    assert all(not kw["token"] for _, kw in hf.calls), hf.calls
    assert TOKEN not in out


def test_files_go_to_the_flat_prefix_layout_the_prefetch_job_reads(h: Harness, sm: ModuleType) -> None:
    """node-ops/model-prefetch syncs ``s3://bucket/<prefix>/`` flat into the snapshot dir and requires ``<prefix>/.done``."""
    hf, s3 = FakeHf(), FakeS3()
    assert h.run(hf, s3)[0] == sm.EXIT_OK
    assert {k for k in s3.keys() if not k.startswith(PREFIX + "/")} == set()
    assert f"{PREFIX}/.done" in s3.keys()


def test_every_upload_uses_upload_file_with_a_sha256_checksum_and_the_right_bucket(h: Harness, sm: ModuleType) -> None:
    hf, s3 = FakeHf(), FakeS3()
    assert h.run(hf, s3)[0] == sm.EXIT_OK
    weights = [u for u in s3.uploads if u["Key"].endswith(".safetensors")]
    assert len(weights) == 2
    assert all(u["Bucket"] == BUCKET for u in s3.uploads)
    assert all(u["ExtraArgs"].get("ChecksumAlgorithm") == "SHA256" for u in weights), s3.uploads


# ----------------------------------------------------------------------------------- file selection
def test_selection_excludes_noise_and_bin_duplicates_when_safetensors_exist(h: Harness, sm: ModuleType) -> None:
    hf, s3 = FakeHf(), FakeS3()
    assert h.run(hf, s3)[0] == sm.EXIT_OK
    got = staged(s3)
    for noise in ("README.md", ".gitattributes", "pytorch_model-00001-of-00002.bin", "pytorch_model.bin.index.json", "tf_model.h5", "flax_model.msgpack"):
        assert noise not in got, f"{noise} must not be staged"
    downloaded = {kw["filename"] for kw in hf.names("hf_hub_download")}
    assert downloaded == WANTED, "noise is never even downloaded"


def test_bin_weights_are_kept_when_the_repo_has_no_safetensors(h: Harness, sm: ModuleType) -> None:
    content = {p: d for p, d in CONTENT.items() if ".safetensors" not in p and p not in ("tf_model.h5", "flax_model.msgpack")}
    hf, s3 = FakeHf(content=content, lfs_paths={"pytorch_model-00001-of-00002.bin"}), FakeS3()
    rc, out = h.run(hf, s3)
    assert rc == sm.EXIT_OK, out
    assert "pytorch_model-00001-of-00002.bin" in staged(s3)
    assert "config.json" in staged(s3)



@pytest.mark.parametrize("path", ["LICENSE", "LICENSE.txt", "LICENSE.md", "NOTICE", "COPYING", "USE_POLICY.md"])
def test_licence_files_are_staged_with_or_without_an_extension(sm: ModuleType, path: str) -> None:
    """Live test 2 (D4): an extensionless ``LICENSE`` was left out of the staged model."""
    tree = [tree_entry("model.safetensors", b"W" * 64, lfs=True), tree_entry(path, b"terms", lfs=False)]
    assert path in {f.path for f in sm.select_files(tree)}


@pytest.mark.parametrize("path", ["Makefile", "Dockerfile", "notes"])
def test_other_extensionless_files_are_not_staged(sm: ModuleType, path: str) -> None:
    tree = [tree_entry("model.safetensors", b"W" * 64, lfs=True), tree_entry(path, b"x", lfs=False)]
    assert {f.path for f in sm.select_files(tree)} == {"model.safetensors"}

# ----------------------------------------------------------------------------------- gated handling
@pytest.mark.parametrize("gated", ["auto", "manual"])
def test_gated_without_token_fails_fast_with_actionable_message(h: Harness, sm: ModuleType, gated: str) -> None:
    hf, s3 = FakeHf(gated=gated, needs_token=True), FakeS3()
    rc, out = h.run(hf, s3)
    assert rc == sm.EXIT_GATED_NO_TOKEN, out
    assert "HF_TOKEN" in out
    assert re.search(r"licen[cs]e", out, re.I), "tell the user to accept the licence on the model page"
    assert MODEL in out
    # gen_env.py overwrites config/sample.env, so a token there is ignored (tests/scripts/test_153): the message
    # must send the reader to the shell, never to sample.env.
    assert re.search(r"export\s+HF_TOKEN", out), "tell the user to export HF_TOKEN in the shell"
    assert "sample.env" not in out, "a token in the generated sample.env is ignored; do not point the reader there"
    assert hf.names("hf_hub_download") == [] and hf.names("list_repo_tree") == [], "fail fast: nothing is listed or downloaded"
    assert s3.write_ops() == [], "nothing is written to S3"


@pytest.mark.parametrize("gated", ["auto", "manual"])
def test_gated_with_valid_token_sends_the_token_and_stages(h: Harness, sm: ModuleType, gated: str) -> None:
    hf, s3 = FakeHf(gated=gated, needs_token=True), FakeS3()
    rc, out = h.run(hf, s3, HF_TOKEN=TOKEN)
    assert rc == sm.EXIT_OK, out
    assert staged(s3) == WANTED
    assert {kw["token"] for kw in hf.names("hf_hub_download")} == {TOKEN}
    assert {kw["token"] for kw in hf.names("list_repo_tree")} == {TOKEN}
    assert TOKEN not in out


@pytest.mark.parametrize("raiser", ["list_repo_tree", "hf_hub_download"])
def test_gated_with_token_but_licence_not_accepted_is_a_distinct_error(h: Harness, sm: ModuleType, raiser: str) -> None:
    """GatedRepoError (403) subclasses RepositoryNotFoundError: it must not be reported as 'not found'."""
    hf, s3 = FakeHf(gated="manual", raises={raiser: gated_error()}), FakeS3()
    rc, out = h.run(hf, s3, HF_TOKEN=TOKEN)
    assert rc == sm.EXIT_ACCESS_DENIED, out
    assert rc not in (sm.EXIT_GATED_NO_TOKEN, sm.EXIT_NOT_FOUND)
    assert "HF_TOKEN" in out and re.search(r"licen[cs]e|access", out, re.I)
    assert "not found" not in out.lower()
    assert TOKEN not in out


def test_invalid_token_401_is_reported_as_access_denied(h: Harness, sm: ModuleType) -> None:
    hf, s3 = FakeHf(gated="auto", raises={"list_repo_tree": unauthorized_error()}), FakeS3()
    rc, out = h.run(hf, s3, HF_TOKEN=TOKEN)
    assert rc == sm.EXIT_ACCESS_DENIED, out
    assert TOKEN not in out


def test_messages_of_the_three_gated_failures_are_all_different(h: Harness, sm: ModuleType, tmp_path: Path) -> None:
    outs = []
    hf = FakeHf(gated="auto", needs_token=True)
    outs.append(h.run(hf, FakeS3())[1])
    hf = FakeHf(gated="auto", raises={"list_repo_tree": gated_error()})
    outs.append(h.run(hf, FakeS3(), HF_TOKEN=TOKEN)[1])
    hf = FakeHf(raises={"model_info": not_found_error()})
    outs.append(h.run(hf, FakeS3())[1])
    assert len(set(outs)) == 3


# ----------------------------------------------------------------------------------- repo not found
@pytest.mark.parametrize("token", [None, TOKEN])
def test_repo_not_found(h: Harness, sm: ModuleType, token: str | None) -> None:
    hf, s3 = FakeHf(raises={"model_info": not_found_error()}), FakeS3()
    rc, out = h.run(hf, s3, HF_TOKEN=token)
    assert rc == sm.EXIT_NOT_FOUND, out
    assert "not found" in out.lower() and MODEL in out
    assert s3.write_ops() == []
    assert TOKEN not in out


# ----------------------------------------------------------------------------------- revision / config validation
@pytest.mark.parametrize("revision", ["main", "refs/pr/1", "v1.0", "0123456", "0123456789ABCDEF0123456789ABCDEF01234567", "0123456789abcdef0123456789abcdef0123456", "", "g" * 40])
def test_revision_must_be_a_40_hex_commit_sha(h: Harness, sm: ModuleType, revision: str) -> None:
    hf, s3 = FakeHf(), FakeS3()
    rc, out = h.run(hf, s3, MODEL_REVISION=revision)
    assert rc == sm.EXIT_CONFIG, out
    assert "MODEL_REVISION" in out
    assert hf.calls == [] and s3.ops == [], "validated before any network call"


@pytest.mark.parametrize(
    ("name", "value"),
    [("MODEL_ID", None), ("MODEL_ID", "no-slash"), ("MODEL_ID", "a/b/c"), ("MODEL_REVISION", None), ("MODEL_S3_URI", None), ("MODEL_S3_URI", "https://bucket/key"), ("MODEL_S3_URI", "s3://bucket-only")],
)
def test_invalid_or_missing_config_is_rejected_before_any_call(h: Harness, sm: ModuleType, name: str, value: str | None) -> None:
    hf, s3 = FakeHf(), FakeS3()
    rc, out = h.run(hf, s3, **{name: value})
    assert rc == sm.EXIT_CONFIG, out
    assert name in out
    assert hf.calls == [] and s3.ops == []


@pytest.mark.parametrize("model_id", ["../x", "./x", "x/..", "x/.", "../..", "/x", "x/", "x//y"])
def test_path_ish_model_id_is_rejected_before_any_call(h: Harness, sm: ModuleType, model_id: str) -> None:
    """The default S3 prefix is <MODEL_ID>/<MODEL_REVISION>: '.', '..' and empty parts are refused."""
    hf, s3 = FakeHf(), FakeS3()
    rc, out = h.run(hf, s3, MODEL_ID=model_id)
    assert rc == sm.EXIT_CONFIG, out
    assert "MODEL_ID" in out
    assert hf.calls == [] and s3.ops == []


@pytest.mark.parametrize("model_id", ["Qwen/Qwen3.8-27B-FP8", "acme/tiny_model.v2"])
def test_dotted_model_names_are_valid(sm: ModuleType, tmp_path: Path, model_id: str) -> None:
    assert sm.load_config(env(MODEL_ID=model_id), tmp_path).model_id == model_id


# ----------------------------------------------------------------------------------- idempotency / resume
def test_done_marker_of_the_same_revision_exits_zero_without_touching_hf_or_uploading(h: Harness, sm: ModuleType) -> None:
    hf, s3 = FakeHf(), FakeS3()
    assert h.run(hf, s3)[0] == sm.EXIT_OK
    hf2, uploads_before = FakeHf(gated="manual", needs_token=True), len(s3.uploads)
    s3.ops.clear()
    rc, out = h.run(hf2, s3)  # a gated model and no token: irrelevant once the marker is there
    assert rc == sm.EXIT_OK, out
    assert hf2.calls == [], "exits immediately: no HF call at all"
    assert len(s3.uploads) == uploads_before and s3.write_ops() == []


def test_done_marker_of_another_revision_does_not_short_circuit(h: Harness, sm: ModuleType) -> None:
    other = "f" * 40
    s3 = FakeS3(objects={f"{PREFIX}/.done": f'{{"model_id": "{MODEL}", "revision": "{other}", "file_count": 8}}'.encode()})
    hf = FakeHf()
    rc, out = h.run(hf, s3)
    assert rc == sm.EXIT_OK, out
    assert hf.names("hf_hub_download"), "a stale marker must not skip the download"
    assert REVISION in marker_fields(s3.objects[f"{PREFIX}/.done"]).values()


def test_files_already_in_s3_with_the_same_size_are_skipped_and_not_downloaded(h: Harness, sm: ModuleType) -> None:
    first = "model-00001-of-00002.safetensors"
    s3 = FakeS3(objects={f"{PREFIX}/{first}": CONTENT[first]})
    hf = FakeHf()
    rc, out = h.run(hf, s3)
    assert rc == sm.EXIT_OK, out
    assert first not in {kw["filename"] for kw in hf.names("hf_hub_download")}
    assert f"{PREFIX}/{first}" not in {u["Key"] for u in s3.uploads}
    assert staged(s3) == WANTED


def test_a_partial_object_of_the_wrong_size_is_replaced(h: Harness, sm: ModuleType) -> None:
    first = "model-00001-of-00002.safetensors"
    s3 = FakeS3(objects={f"{PREFIX}/{first}": CONTENT[first][:10]})
    rc, out = h.run(FakeHf(), s3)
    assert rc == sm.EXIT_OK, out
    assert s3.objects[f"{PREFIX}/{first}"] == CONTENT[first]


def test_a_failed_run_can_be_resumed_without_repeating_finished_files(h: Harness, sm: ModuleType) -> None:
    s3 = FakeS3()
    hf_bad = FakeHf(raises={"hf_hub_download": RuntimeError("connection reset")})
    rc, _ = h.run(hf_bad, s3)
    assert rc == sm.EXIT_TRANSFER and f"{PREFIX}/.done" not in s3.keys()
    hf_ok = FakeHf()
    rc, out = h.run(hf_ok, s3)
    assert rc == sm.EXIT_OK, out
    assert staged(s3) == WANTED


# ----------------------------------------------------------------------------------- verification
def test_s3_size_mismatch_after_upload_fails_and_writes_no_marker(h: Harness, sm: ModuleType) -> None:
    s3 = FakeS3(truncate_to={"model-00002-of-00002.safetensors": 100})
    rc, out = h.run(FakeHf(), s3)
    assert rc == sm.EXIT_VERIFY, out
    assert "model-00002-of-00002.safetensors" in out and "2048" in out and "100" in out, "report the file and both sizes"
    assert f"{PREFIX}/.done" not in s3.keys()


def test_lfs_sha256_mismatch_fails_before_upload_and_writes_no_marker(h: Harness, sm: ModuleType) -> None:
    bad = "model-00001-of-00002.safetensors"
    hf = FakeHf(corrupt={bad: b"Z" * len(CONTENT[bad])})  # same size, different bytes: only the sha256 can catch it
    s3 = FakeS3()
    rc, out = h.run(hf, s3)
    assert rc == sm.EXIT_VERIFY, out
    assert bad in out and "sha256" in out.lower()
    assert f"{PREFIX}/{bad}" not in s3.keys(), "a corrupt file is never uploaded"
    assert f"{PREFIX}/.done" not in s3.keys()


def test_download_size_mismatch_against_the_api_fails(h: Harness, sm: ModuleType) -> None:
    bad = "config.json"
    hf = FakeHf(corrupt={bad: b"{}"})
    s3 = FakeS3()
    rc, out = h.run(hf, s3)
    assert rc == sm.EXIT_VERIFY, out
    assert f"{PREFIX}/.done" not in s3.keys()


def test_verification_paginates_the_s3_listing(h: Harness, sm: ModuleType) -> None:
    s3 = FakeS3(page_size=1)
    rc, out = h.run(FakeHf(), s3)
    assert rc == sm.EXIT_OK, out
    assert len([o for o in s3.ops if o[0] == "list"]) > 1, "more than one page has to be read"


# ----------------------------------------------------------------------------------- disk bound
def test_one_file_at_a_time_on_disk_and_nothing_left_behind(h: Harness, sm: ModuleType) -> None:
    hf, s3 = FakeHf(), FakeS3()
    assert h.run(hf, s3)[0] == sm.EXIT_OK
    assert hf.resident_peak == 1, "stream per file: the disk need is the largest shard, not the whole model"
    assert h.leftovers() == []


# ----------------------------------------------------------------------------------- token hygiene
def exploding(token: str | None) -> Exception:
    """An exception whose text carries the token (HTTP libraries do this with request headers and URLs)."""
    return RuntimeError(f"request failed: Authorization: Bearer {token} url=https://huggingface.co/x?token={token}")


@pytest.mark.parametrize("raiser", ["model_info", "list_repo_tree", "hf_hub_download"])
def test_token_in_an_exception_message_is_never_logged(h: Harness, sm: ModuleType, raiser: str) -> None:
    hf, s3 = FakeHf(gated="auto", raises={raiser: exploding}), FakeS3()
    rc, out = h.run(hf, s3, HF_TOKEN=TOKEN)
    assert rc != sm.EXIT_OK
    assert TOKEN not in out, "the token must be redacted from every message and traceback"


def test_token_never_reaches_s3_marker_or_metadata(h: Harness, sm: ModuleType) -> None:
    hf, s3 = FakeHf(gated="auto", needs_token=True), FakeS3()
    assert h.run(hf, s3, HF_TOKEN=TOKEN)[0] == sm.EXIT_OK
    assert all(TOKEN.encode() not in v for v in s3.objects.values())
    assert all(TOKEN not in repr(u) for u in s3.uploads)


@pytest.mark.parametrize("token", [" ", "tok en", "tok\nen", "a" * 4097], ids=["blank", "space", "newline", "too-long"])
def test_malformed_token_is_a_config_error_and_is_not_echoed(h: Harness, sm: ModuleType, token: str) -> None:
    rc, out = h.run(FakeHf(gated="auto"), FakeS3(), HF_TOKEN=token)
    assert rc == sm.EXIT_CONFIG, out
    assert "HF_TOKEN" in out
    if len(token) < 100:
        assert repr(token) not in out


@pytest.mark.parametrize(
    ("gated", "token", "auth"),
    [("manual", TOKEN, "bearer"), ("auto", TOKEN, "bearer"), (False, TOKEN, "none"), (False, None, "none")],
    ids=["gated-token", "auto-token", "public-token-ignored", "public-no-token"],
)
def test_model_log_line_says_whether_a_bearer_token_is_sent_without_a_credential_word(
    h: Harness, sm: ModuleType, gated: str | bool, token: str | None, auth: str
) -> None:
    """Static analysis (Semgrep python-logger-credential-disclosure): the field is ``auth=bearer|none``, never ``token=``."""
    hf = FakeHf(gated=gated, needs_token=bool(gated))
    rc, out = h.run(hf, FakeS3(), HF_TOKEN=token)
    assert rc == sm.EXIT_OK, out
    assert f"event=model model={MODEL} revision={REVISION} gated={gated} auth={auth}" in out, out
    assert "token=" not in out, "no log field is named token"
    assert TOKEN not in out


def test_no_logger_format_string_matches_the_credential_heuristic() -> None:
    """The format-string regex of Semgrep python.lang.security.audit.logging.python-logger-credential-disclosure."""
    src = MODULE_PATH.read_text()
    formats = re.findall(r"\bLOG\.(?:debug|info|warn|warning|error|exception|critical)\(\s*\"([^\"]*)\"", src)
    assert formats, "the module logs through LOG"
    heuristic = re.compile(r"(?i).*(api.key|secret|credential|token|password).*\%s.*")
    assert [f for f in formats if heuristic.search(f)] == []


def test_empty_token_counts_as_unset(h: Harness, sm: ModuleType) -> None:
    """The optional secretKeyRef can yield an empty string."""
    hf = FakeHf(gated="auto", needs_token=True)
    rc, _ = h.run(hf, FakeS3(), HF_TOKEN=EMPTY)
    assert rc == sm.EXIT_GATED_NO_TOKEN
    rc, _ = h.run(FakeHf(gated=False), FakeS3(), HF_TOKEN=EMPTY)
    assert rc == sm.EXIT_OK


# ----------------------------------------------------------------------------------- pins and source hygiene
def test_requirements_pin_exact_versions_the_contract_was_validated_with() -> None:
    req = TOOL / "requirements.txt"
    assert req.is_file(), "tools/stage_model/requirements.txt must exist (it is mounted in the Job through the ConfigMap)"
    lines = [ln.strip() for ln in req.read_text().splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    assert all(re.fullmatch(r"[A-Za-z0-9_.\[\]-]+==\d+(\.\d+)*", ln) for ln in lines), f"every requirement must be pinned with ==: {lines}"
    pins = {ln.split("==")[0].lower().replace("-", "_"): ln.split("==")[1] for ln in lines}
    assert pins.get("huggingface_hub") == "2.3.0"
    assert pins.get("boto3") == "1.43.118"
    assert "hf_transfer" not in pins, "hf_transfer was removed in huggingface_hub 1.x; hf_xet (a dependency) is the fast path"


def test_source_does_not_use_removed_or_unsafe_apis() -> None:
    src = MODULE_PATH.read_text()
    assert "hf_transfer" not in src.lower() and "HF_HUB_ENABLE_HF_TRANSFER" not in src
    assert "snapshot_download" not in src, "per-file download keeps the disk bounded"
    assert not re.search(r"(?m)^\s*(print\(|logging\.\w+\().*HF_TOKEN", src), "never log the token variable"
    assert "token=True" not in src and "login(" not in src
    assert not re.search(r":latest\b", src)
