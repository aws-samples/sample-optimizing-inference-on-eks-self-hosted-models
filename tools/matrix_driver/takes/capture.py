"""Record the Grafana demo-take view for one take and upload it to S3.

Vendored for the E7' takes of the Qwen3.8 matrix driver v4 (ConfigMap q38-take-capture, Job bench/demo-capture-
<take_id> created by matrix_driver.py) from the repo's manifests/demo/capture/capture.py (commit
261fe39, blob 460d5d6). Changes: the key prefix comes from S3_PREFIX (the driver sets takes/<run_id>; the block-1
constant is only the default), uploads are append-only (an existing key fails the Job), and <TAKE_ID>-capture.json
records when the video starts and ends (epoch ms) so the compose Job can sync the webm on the replay start.
v4 critic fixes: before recording, the Prometheus datasource is queried through Grafana for scraped targets of
NAMESPACE (a dashboard that renders its titles over a dead datasource would record empty panels; exit 4 when none
within DATA_WAIT_S); the PNG frames go up as <TAKE_ID>-frames-NNNN.tar chunks every FRAMES_CHUNK_S during the
recording (a crash keeps the evidence so far); an empty webm is refused; capture.json carries the sizes and the
data check, and is uploaded (status failed) also when the recording fails; CHECK_ONLY=1 is the launch preflight
(Job q38-take-preflight-<run_id>): Grafana login, dashboard + namespace variable, datasource, the namespace label
values of CHECK_NAMESPACES and one Chromium render of the panel, reported to S3 without recording.

Runs in the demo capture Job (manifests/demo/capture). A headless Chromium opens the dashboard
DASHBOARD_UID through the in-cluster Grafana Service with the absolute range of the take
(FROM_MS..TO_MS, kiosk, refresh 5 s, var-namespace=NAMESPACE), authenticated with an HTTP basic
header (GRAFANA_USER / GRAFANA_PASSWORD, never typed into the login form, never printed). It records
a 1920x1080 webm for DURATION_S seconds and saves a PNG every INTERVAL_S seconds as a fallback, then
uploads the webm, the frame chunks and the timing file to s3://RESULTS_BUCKET/S3_PREFIX/TAKE_ID/
(AWS CLI when present, else boto3). Exit codes: 0 uploaded (CHECK_ONLY: every check passed), 1 bad configuration,
2 no panel rendered within PANEL_WAIT_S, 3 capture or upload failure, 4 Grafana/Prometheus data check failed.
Static analysis audit: GRAFANA_URL must be http(s) with a host (no redirect to another scheme is followed), and the settings
that reach the aws CLI argv or an S3 key (RESULTS_BUCKET, AWS_REGION, TAKE_ID, S3_PREFIX) must be plain names, OUT_DIR
absolute; anything else is a configuration error (exit 1) before any request, file or process.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from http.client import HTTPMessage
from pathlib import Path
from typing import IO, Any

# The render check waits for this panel title of the demo-take dashboard (overridable).
DEFAULT_PANEL_TITLE = "TTFT p95 since replay start"
DEFAULT_S3_PREFIX = "kubecon-demo-capture"  # block 1; the v4 driver sets S3_PREFIX=takes/<run_id>
# The dashboard's namespace variable: label_values(<this metric>, namespace) (dashboard demo-take, checked live).
NAMESPACE_METRIC = "dynamo_frontend_time_to_first_token_seconds_count"
VIEW = {"width": 1920, "height": 1080}
# Values that must never reach a log line (the password and its basic-auth encoding).
_SECRETS: list[str] = []
HTTP_SCHEMES = frozenset({"http", "https"})
# Settings that reach the aws CLI argv or an S3 key: none admits a leading '-' (an option), whitespace or a shell
# character; a key segment never starts with '.', so '.' and '..' are refused, and a prefix joins segments with '/'.
BUCKET_RE = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")
REGION_RE = re.compile(r"[a-z]+(-[a-z]+)*-[0-9]+")
_SEGMENT = r"[A-Za-z0-9_][A-Za-z0-9._-]*"
TAKE_RE = re.compile(_SEGMENT)
PREFIX_RE = re.compile(rf"{_SEGMENT}(/{_SEGMENT})*")


class ConfigError(Exception):
    """A required setting is missing or malformed."""


class CheckError(Exception):
    """Grafana or its Prometheus datasource answered something unusable."""


def log(message: str) -> None:
    """Print one progress line (never a secret).

    Args:
        message: Text to print.
    """
    for secret in _SECRETS:
        message = message.replace(secret, "***")
    print(f"capture: {message}", flush=True)


def setting(name: str, default: str | None = None) -> str:
    """Return an environment setting; an empty value means the default.

    Args:
        name: Environment variable name.
        default: Value used when the variable is unset or empty; None makes it required.

    Returns:
        The value.

    Raises:
        ConfigError: If the setting is required and missing.
    """
    value = os.environ.get(name, "").strip()
    if value:
        return value
    if default is None:
        raise ConfigError(f"{name} is not set")
    return default


def whole(name: str, default: str | None = None, minimum: int = 0) -> int:
    """Return an integer setting.

    Args:
        name: Environment variable name.
        default: Default value (as text); None makes it required.
        minimum: Smallest accepted value.

    Returns:
        The integer value.

    Raises:
        ConfigError: If the value is not a whole number >= minimum.
    """
    raw = setting(name, default)
    if not raw.isdigit() or int(raw) < minimum:
        raise ConfigError(f"{name} must be a whole number >= {minimum}, got {raw!r}")
    return int(raw)


def plain(name: str, value: str, pattern: re.Pattern[str]) -> str:
    """Return a setting that reaches the aws CLI argv or an S3 key when it fully matches its pattern.

    Args:
        name: Environment variable name (for the message).
        value: The setting.
        pattern: BUCKET_RE, REGION_RE, TAKE_RE or PREFIX_RE.

    Returns:
        The value, unchanged.

    Raises:
        ConfigError: If the value does not match.
    """
    if not pattern.fullmatch(value):
        raise ConfigError(f"{name} must match {pattern.pattern}, got {value!r}")
    return value


def http_url(url: str) -> str:
    """Return ``url`` when it is an http(s) URL with a host (the only kind capture.py opens).

    Args:
        url: The URL.

    Returns:
        The URL, unchanged.

    Raises:
        ValueError: If the scheme is not http(s) or there is no host (file:, ftp:, data:, a bare path).
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in HTTP_SCHEMES or not parts.netloc:
        raise ValueError(f"refusing a non-http(s) URL {url!r}")
    return url


class _HttpOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Follow redirects to http(s) URLs only (urllib's default handler also follows ``ftp:``)."""

    def redirect_request(self, req: urllib.request.Request, fp: IO[bytes], code: int, msg: str,
                         headers: HTTPMessage, newurl: str) -> urllib.request.Request | None:
        """Refuse a redirect to another scheme; delegate the rest to urllib.

        Args:
            req: The redirected request.
            fp: The redirect response body.
            code: The 3xx status.
            msg: The status reason.
            headers: The redirect response headers.
            newurl: The absolute redirect target.

        Returns:
            The follow-up request, or None when urllib does not follow this redirect.

        Raises:
            urllib.error.HTTPError: The target is not an http(s) URL (carries the 3xx code; not followed).
        """
        if urllib.parse.urlsplit(newurl).scheme not in HTTP_SCHEMES:
            raise urllib.error.HTTPError(newurl, code, f"{msg} - redirect to a non-http(s) URL refused", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# urllib's default opener with the redirect handler above: with http_url, no URL other than http(s) is ever opened.
_OPENER = urllib.request.build_opener(_HttpOnlyRedirects)


def dashboard_url(base: str, uid: str, from_ms: int, to_ms: int, namespace: str) -> str:
    """Build the kiosk URL of the take view.

    Args:
        base: Grafana base URL.
        uid: Dashboard uid.
        from_ms: Range start, epoch milliseconds.
        to_ms: Range end, epoch milliseconds.
        namespace: Value of the dashboard namespace variable.

    Returns:
        The URL (no credentials in it).
    """
    return f"{base.rstrip('/')}/d/{uid}?orgId=1&from={from_ms}&to={to_ms}&kiosk&refresh=5s&var-namespace={namespace}"


def epoch_ms() -> int:
    """Current wall-clock instant in epoch milliseconds."""
    return round(time.time() * 1000)


def grafana_json(base: str, path: str, auth: str, timeout: float = 15) -> Any:
    """GET a Grafana HTTP API path with the basic-auth header and decode its JSON.

    Args:
        base: Grafana base URL (in-cluster Service).
        path: API path with its query string.
        auth: Value of the Authorization header.
        timeout: Seconds before the request fails.

    Returns:
        The decoded JSON body.

    Raises:
        ValueError: The URL is not http(s) (checked before anything is opened).
        urllib.error.HTTPError: An error status, or a redirect to a non-http(s) URL (not followed).
    """
    url = http_url(base.rstrip("/") + path)  # the Job's own GRAFANA_URL (http in-cluster)
    req = urllib.request.Request(url, headers={"Authorization": auth, "Accept": "application/json"})
    with _OPENER.open(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def prometheus_uid(base: str, auth: str) -> str:
    """Uid of the Prometheus datasource the dashboard's ``datasource`` variable resolves to (the default one).

    Raises:
        CheckError: Grafana has no Prometheus datasource.
    """
    proms = [d for d in grafana_json(base, "/api/datasources", auth) if d.get("type") == "prometheus"]
    if not proms:
        raise CheckError("no Prometheus datasource in Grafana")
    return str(next((d for d in proms if d.get("isDefault")), proms[0])["uid"])


def prom_api(base: str, auth: str, uid: str, path: str, params: dict[str, str]) -> Any:
    """One Prometheus HTTP API call through the Grafana datasource proxy (``data`` of a success answer).

    Raises:
        CheckError: Prometheus did not answer success.
    """
    doc = grafana_json(base, f"/api/datasources/proxy/uid/{uid}{path}?{urllib.parse.urlencode(params)}", auth)
    if doc.get("status") != "success":
        raise CheckError(f"Prometheus {path} answered {doc.get('status')!r}: {str(doc.get('error'))[:200]}")
    return doc["data"]


def up_targets(base: str, auth: str, uid: str, namespace: str | None) -> int:
    """Number of scraped targets up (in ``namespace``, or anywhere), through the Grafana datasource."""
    sel = f'{{namespace="{namespace}"}}' if namespace else ""
    result = prom_api(base, auth, uid, "/api/v1/query", {"query": f"count(up{sel} == 1)"})["result"]
    return int(float(result[0]["value"][1])) if result else 0


def data_check(base: str, auth: str, namespace: str, wait_s: int) -> dict[str, Any]:
    """Prove the panels will have data: Prometheus scrapes targets of the take's namespace, through Grafana.

    Args:
        base: Grafana base URL.
        auth: Value of the Authorization header.
        namespace: The take's namespace (dashboard variable).
        wait_s: Seconds to retry (every 10 s) a datasource that is down, warming or has no target up yet.

    Returns:
        ok, and the datasource uid + targets up, or the last reason.
    """
    deadline = time.monotonic() + wait_s
    reason = ""
    while True:
        try:
            uid = prometheus_uid(base, auth)
            n = up_targets(base, auth, uid, namespace)
            if n > 0:
                return {"ok": True, "datasource_uid": uid, "namespace": namespace, "up_targets": n}
            reason = f"no scraped target up in namespace {namespace}"
        except (OSError, ValueError, KeyError, IndexError, TypeError, CheckError) as exc:  # URLError is an OSError
            reason = f"{type(exc).__name__}: {exc}"
        if time.monotonic() >= deadline:
            return {"ok": False, "namespace": namespace, "reason": reason[:300]}
        log(f"data check: {reason}; retrying")
        time.sleep(10)


def record(url: str, auth: str, *, title: str, panel_wait_s: int, duration_s: int, interval_s: int,
           frames: Path, video_dir: Path, timing: dict[str, int], chunk_s: int = 0,
           on_chunk: Callable[[], None] | None = None) -> int:
    """Open the view, wait for a known panel, then record the webm and the PNG frames.

    Args:
        url: Dashboard URL.
        auth: Value of the Authorization header.
        title: Panel title that proves the dashboard rendered.
        panel_wait_s: Seconds to wait for that title.
        duration_s: Recording length in seconds.
        interval_s: Seconds between two PNG frames.
        frames: Directory for the PNG frames.
        video_dir: Directory where Playwright writes the webm.
        timing: Filled with video_t0_ms (page created: Playwright starts the video), panel_rendered_ms and
            video_end_ms (context closed: the webm is finalized), epoch milliseconds.
        chunk_s: Seconds between two on_chunk calls during the recording (0: never).
        on_chunk: Uploads the frames saved so far (called from this loop, between two screenshots).

    Returns:
        0 when recorded, 2 when no panel rendered, 3 when no frame could be saved.
    """
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(
            viewport=dict(VIEW), record_video_dir=str(video_dir), record_video_size=dict(VIEW),
            extra_http_headers={"Authorization": auth}, color_scheme="dark",
        )
        try:
            timing["video_t0_ms"] = epoch_ms()
            page = context.new_page()
            page.set_default_navigation_timeout(60_000)
            log(f"opening {url}")
            page.goto(url, wait_until="domcontentloaded")
            try:
                page.get_by_text(title, exact=True).first.wait_for(state="visible", timeout=panel_wait_s * 1000)
            except PlaywrightTimeoutError:
                log(f"no panel rendered: {title!r} not visible within {panel_wait_s}s (login or datasource problem?)")
                return 2
            timing["panel_rendered_ms"] = epoch_ms()
            log(f"panel {title!r} rendered; recording {duration_s}s, a frame every {interval_s}s")
            start = time.monotonic()
            last_chunk = start
            saved = 0
            shot = 0
            while True:
                shot += 1
                try:
                    page.screenshot(path=str(frames / f"frame-{shot:05d}.png"))
                    saved += 1
                except Exception as exc:  # noqa: BLE001 - one lost frame must not stop the recording
                    log(f"frame {shot} failed: {type(exc).__name__}")
                if on_chunk is not None and chunk_s > 0 and time.monotonic() - last_chunk >= chunk_s:
                    on_chunk()
                    last_chunk = time.monotonic()
                due = start + shot * interval_s
                if due - start > duration_s:
                    break
                time.sleep(max(0.0, due - time.monotonic()))
            if saved == 0:
                log("no frame could be saved")
                return 3
            log(f"recorded {saved} frames")
            return 0
        finally:
            context.close()  # finalizes the webm
            timing["video_end_ms"] = epoch_ms()
            browser.close()


def render_once(url: str, auth: str, *, title: str, panel_wait_s: int, png: Path) -> bool:
    """Open the view once in Chromium (no video), wait for the panel title and save one PNG (CHECK_ONLY).

    Returns:
        True when the panel title became visible.
    """
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            context = browser.new_context(viewport=dict(VIEW), extra_http_headers={"Authorization": auth},
                                          color_scheme="dark")
            page = context.new_page()
            page.set_default_navigation_timeout(60_000)
            page.goto(url, wait_until="domcontentloaded")
            try:
                page.get_by_text(title, exact=True).first.wait_for(state="visible", timeout=panel_wait_s * 1000)
            except PlaywrightTimeoutError:
                return False
            page.screenshot(path=str(png))
            return True
        finally:
            browser.close()


def aws_cli(argv: list[str], *, capture: bool) -> int:
    """Run one AWS CLI command and return its exit code (the script's one process call site).

    Args:
        argv: ``[<aws resolved by shutil.which>, ...]``: constant subcommands and flags plus RESULTS_BUCKET,
            AWS_REGION and keys of S3_PREFIX/TAKE_ID (all checked by ``main``) and files under the absolute OUT_DIR.
        capture: Keep the output out of the Job log (the head-object probe).

    Returns:
        The exit code.
    """
    # nosemgrep: python.lang.security.audit.dangerous-subprocess-use-audit, dangerous-subprocess-use-audit -- list argv of checked values, resolved aws
    return subprocess.run(argv, check=False, capture_output=capture).returncode  # nosec B603 -- list argv, no shell


def upload(paths: list[Path], bucket: str, prefix: str, region: str) -> bool:
    """Upload files under the given bucket and key prefix (AWS CLI when present, else boto3), append-only.

    Args:
        paths: Files to upload.
        bucket: Destination bucket.
        prefix: Key prefix (no trailing slash).
        region: Bucket region.

    Returns:
        True when every file was uploaded; False on a failure or when a key already exists (never overwritten).
    """
    aws = shutil.which("aws")
    if aws:
        for path in paths:
            dest = f"s3://{bucket}/{prefix}/{path.name}"
            head = [aws, "s3api", "head-object", "--bucket", bucket, "--key", f"{prefix}/{path.name}", "--region", region]
            if aws_cli(head, capture=True) == 0:
                log(f"refusing to overwrite {dest} (append-only)")
                return False
            rc = aws_cli([aws, "s3", "cp", str(path), dest, "--region", region, "--only-show-errors"], capture=False)
            if rc != 0:
                log(f"upload of {path.name} failed (aws rc={rc})")
                return False
            log(f"uploaded {dest}")
        return True
    import boto3
    from botocore.exceptions import ClientError

    client = boto3.client("s3", region_name=region)
    for path in paths:
        try:
            client.head_object(Bucket=bucket, Key=f"{prefix}/{path.name}")
            log(f"refusing to overwrite s3://{bucket}/{prefix}/{path.name} (append-only)")
            return False
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") not in ("404", "NoSuchKey", "NotFound"):
                log(f"cannot check s3://{bucket}/{prefix}/{path.name}: {type(exc).__name__}")
                return False
        try:
            client.upload_file(str(path), bucket, f"{prefix}/{path.name}")
        except Exception as exc:  # noqa: BLE001 - report and fail the Job
            log(f"upload of {path.name} failed: {type(exc).__name__}")
            return False
        log(f"uploaded s3://{bucket}/{prefix}/{path.name}")
    return True


class FrameChunks:
    """The PNG frames of one take as ``<take>-frames-NNNN.tar`` chunks, uploaded (append-only) during the recording;
    a chunk's PNGs are deleted only once it is uploaded (a failed chunk is carried into the next one)."""

    def __init__(self, take: str, frames: Path, out: Path, *, bucket: str, key_prefix: str, region: str) -> None:
        self.take, self.frames, self.out = take, frames, out
        self.bucket, self.key_prefix, self.region = bucket, key_prefix, region
        self.uploaded = self.failed = self.frames_up = self.bytes_up = 0

    def flush(self) -> None:
        """Tar and upload the frames saved since the last uploaded chunk (no-op when none)."""
        pngs = sorted(self.frames.glob("*.png"))
        if not pngs:
            return
        tarball = self.out / f"{self.take}-frames-{self.uploaded + self.failed + 1:04d}.tar"
        with tarfile.open(tarball, "w") as tar:
            for png in pngs:
                tar.add(png, arcname=f"frames/{png.name}")
        size = tarball.stat().st_size
        if upload([tarball], self.bucket, self.key_prefix, self.region):
            self.uploaded += 1
            self.frames_up += len(pngs)
            self.bytes_up += size
            for png in pngs:
                png.unlink()
        else:
            self.failed += 1
            log(f"frame chunk {tarball.name} not uploaded; its frames go with the next chunk")
        tarball.unlink()

    def report(self) -> dict[str, int]:
        """Counters for capture.json."""
        return {"frames": self.frames_up, "frame_chunks": self.uploaded, "frame_chunks_failed": self.failed,
                "frames_bytes": self.bytes_up}


def preflight(base: str, auth: str, *, user: str, uid: str, namespaces: list[str], title: str, panel_wait_s: int,
              png: Path) -> dict[str, Any]:
    """CHECK_ONLY: everything a take needs from Grafana, without recording.

    Returns:
        The report (no secret in it); ``ok`` when every check passed.
    """
    report: dict[str, Any] = {"at_ms": epoch_ms(), "grafana_url": base, "dashboard_uid": uid, "namespaces": namespaces}
    try:
        report["login_is_expected_user"] = grafana_json(base, "/api/user", auth).get("login") == user
        dash = grafana_json(base, f"/api/dashboards/uid/{uid}", auth).get("dashboard") or {}
        names = [v.get("name") for v in (dash.get("templating") or {}).get("list") or []]
        report["dashboard"] = {"title": dash.get("title"), "has_namespace_variable": "namespace" in names}
        ds = prometheus_uid(base, auth)
        report["datasource_uid"] = ds
        report["up_targets_total"] = up_targets(base, auth, ds, None)
        values = prom_api(base, auth, ds, "/api/v1/label/namespace/values", {"match[]": NAMESPACE_METRIC})
        report["namespace_values_missing"] = [ns for ns in namespaces if ns not in values]
    except (OSError, ValueError, KeyError, IndexError, TypeError, CheckError) as exc:
        report["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
    now = epoch_ms()
    url = dashboard_url(base, uid, now - 3_600_000, now, namespaces[0])
    try:
        report["panel_rendered"] = render_once(url, auth, title=title, panel_wait_s=panel_wait_s, png=png)
    except Exception as exc:  # noqa: BLE001 - a browser failure is a failed check
        report["panel_rendered"] = False
        report["render_error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
    report["ok"] = bool("error" not in report and report.get("login_is_expected_user")
                        and report["dashboard"]["has_namespace_variable"] and report["up_targets_total"] > 0
                        and not report["namespace_values_missing"] and report["panel_rendered"])
    return report


def main() -> int:
    """Run one capture (or, with CHECK_ONLY=1, the launch preflight).

    Returns:
        The process exit code.
    """
    try:
        check_only = setting("CHECK_ONLY", "0") == "1"
        take = plain("TAKE_ID", setting("TAKE_ID"), TAKE_RE)
        base = setting("GRAFANA_URL")
        try:
            http_url(base)
        except ValueError:
            raise ConfigError("GRAFANA_URL must be an http(s) URL with a host") from None  # value not echoed
        uid = setting("DASHBOARD_UID", "demo-take")
        user = setting("GRAFANA_USER", "admin")
        password = setting("GRAFANA_PASSWORD")
        bucket = plain("RESULTS_BUCKET", setting("RESULTS_BUCKET"), BUCKET_RE)
        region = setting("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", ""))
        if not region:
            raise ConfigError("AWS_REGION is not set")
        plain("AWS_REGION", region, REGION_RE)
        title = setting("PANEL_TITLE", DEFAULT_PANEL_TITLE)
        prefix = plain("S3_PREFIX", setting("S3_PREFIX", DEFAULT_S3_PREFIX).strip("/"), PREFIX_RE)
        panel_wait_s = whole("PANEL_WAIT_S", "180", 1)
        out_dir = Path(setting("OUT_DIR", "/out"))
        if not out_dir.is_absolute():  # its files are positional arguments of `aws s3 cp`
            raise ConfigError(f"OUT_DIR must be an absolute path, got {str(out_dir)!r}")
        out = out_dir / take
        if check_only:
            namespaces = [n.strip() for n in setting("CHECK_NAMESPACES").split(",") if n.strip()]
            from_ms = to_ms = duration_s = interval_s = data_wait_s = chunk_s = 0
            namespace = namespaces[0] if namespaces else ""
        else:
            namespaces = []
            from_ms, to_ms = whole("FROM_MS"), whole("TO_MS")
            namespace = setting("NAMESPACE")
            duration_s = whole("DURATION_S", "1020", 1)
            interval_s = whole("INTERVAL_S", "2", 1)
            data_wait_s = whole("DATA_WAIT_S", "120", 0)
            chunk_s = whole("FRAMES_CHUNK_S", "300", 0)
    except ConfigError as exc:
        log(f"configuration error: {exc}")
        return 1
    if not check_only and to_ms <= from_ms:
        log("configuration error: TO_MS must be after FROM_MS")
        return 1
    if check_only and not namespace:
        log("configuration error: CHECK_NAMESPACES is empty")
        return 1

    out.mkdir(parents=True, exist_ok=True)
    encoded = base64.b64encode(f"{user}:{password}".encode()).decode()
    _SECRETS.extend([password, encoded])
    auth = "Basic " + encoded

    if check_only:
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        png = out / f"{take}-{stamp}.png"
        report = preflight(base, auth, user=user, uid=uid, namespaces=namespaces, title=title,
                           panel_wait_s=panel_wait_s, png=png)
        log(f"preflight: {json.dumps(report, sort_keys=True)}")
        meta = out / f"{take}-{stamp}.json"
        meta.write_text(json.dumps(report, indent=1, sort_keys=True))
        if not upload([meta, *([png] if png.exists() else [])], bucket, prefix, region):
            return 3
        return 0 if report["ok"] else 4

    check = data_check(base, auth, namespace, data_wait_s)
    log(f"data check: {json.dumps(check, sort_keys=True)}")
    frames, video_dir = out / "frames", out / "video"
    frames.mkdir(parents=True, exist_ok=True)
    video_dir.mkdir(parents=True, exist_ok=True)
    key_prefix = f"{prefix}/{take}"
    chunks = FrameChunks(take, frames, out, bucket=bucket, key_prefix=key_prefix, region=region)
    timing: dict[str, int] = {}
    meta = out / f"{take}-capture.json"

    def write_meta(status: str, **extra: Any) -> None:
        meta.write_text(json.dumps({"take_id": take, "status": status, **timing, "from_ms": from_ms, "to_ms": to_ms,
                                    "namespace": namespace, "dashboard_uid": uid, "duration_s": duration_s,
                                    "interval_s": interval_s, "data_check": check, **chunks.report(), **extra},
                                   indent=1))

    def fail(rc: int, reason: str) -> int:
        """Keep the evidence so far (last frame chunk + capture.json, status failed), best effort."""
        chunks.flush()
        write_meta("failed", reason=reason[:300])
        upload([meta], bucket, key_prefix, region)
        return rc

    if not check["ok"]:
        return fail(4, f"data check failed: {check.get('reason')}")
    url = dashboard_url(base, uid, from_ms, to_ms, namespace)
    try:
        rc = record(url, auth, title=title, panel_wait_s=panel_wait_s, duration_s=duration_s, interval_s=interval_s,
                    frames=frames, video_dir=video_dir, timing=timing, chunk_s=chunk_s, on_chunk=chunks.flush)
    except Exception as exc:  # noqa: BLE001 - browser failures end the Job with a clear code
        log(f"capture failed: {type(exc).__name__}: {exc}")
        return fail(3, f"capture failed: {type(exc).__name__}")
    if rc != 0:
        return fail(rc, "no panel rendered" if rc == 2 else "no frame saved")

    chunks.flush()
    videos = sorted(video_dir.glob("*.webm"))
    if not videos or videos[-1].stat().st_size == 0:
        log("capture failed: Playwright wrote no webm (or an empty one)")
        return fail(3, "no webm or an empty webm")
    webm = out / f"{take}.webm"
    videos[-1].replace(webm)
    write_meta("ok", webm_bytes=webm.stat().st_size)
    if not upload([webm, meta], bucket, key_prefix, region):  # capture.json last: the take's media are complete
        return 3
    log(f"done: s3://{bucket}/{key_prefix}/ (webm {webm.stat().st_size} bytes, {chunks.report()})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
