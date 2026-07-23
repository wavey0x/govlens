"""Publish one Wavey Gist and verify its exact public revision."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

API_BASE = "https://api.wavey.info/api/v1"
PUBLIC_BASE = "https://gist.wavey.info"
GIST_ID = re.compile(r"^[A-Za-z0-9]{16,64}$")
GIST_PATH = re.compile(r"^/[A-Za-z0-9]{16,64}$")
MAX_REPORT_BYTES = 64 * 1024
REPORT_FILENAME = "README.md"


class GistError(RuntimeError):
    """Publication conclusively failed before a Gist was accepted."""


class GistUnknown(RuntimeError):
    """Publication may have succeeded and must not be repeated automatically."""

    def __init__(self, message: str, url: str | None = None) -> None:
        super().__init__(message)
        self.url = url


@dataclass(frozen=True)
class PublishedGist:
    url: str
    revision: int
    sha256: str


def validate_gist_url(url: str) -> str:
    parsed = urlparse(url)
    try:
        valid = (
            parsed.scheme == "https"
            and parsed.hostname == "gist.wavey.info"
            and parsed.port is None
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
            and bool(GIST_PATH.fullmatch(parsed.path))
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("invalid Wavey Gist URL")
    return url


def _snapshot_sha256(title: str, content_sha256: str) -> str:
    manifest = {
        "version": 1,
        "title": title,
        "files": [
            {
                "filename": REPORT_FILENAME,
                "content_sha256": content_sha256,
            }
        ],
    }
    encoded = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _matches_snapshot(
    body: object,
    *,
    title: str,
    markdown: str,
    content_sha256: str,
    snapshot_sha256: str,
) -> bool:
    if not isinstance(body, dict):
        return False
    files = body.get("files")
    if not isinstance(files, dict) or set(files) != {REPORT_FILENAME}:
        return False
    report = files.get(REPORT_FILENAME)
    return (
        isinstance(report, dict)
        and body.get("title") == title
        and body.get("primary_file") == REPORT_FILENAME
        and body.get("snapshot_sha256") == snapshot_sha256
        and report.get("filename") == REPORT_FILENAME
        and report.get("content") == markdown
        and report.get("content_sha256") == content_sha256
        and report.get("byte_size") == len(markdown.encode("utf-8"))
    )


class Gist:
    def __init__(self, key: str, *, client: httpx.Client | None = None) -> None:
        self.key = key
        self.client = client or httpx.Client(timeout=20, follow_redirects=False)
        self.owns_client = client is None

    @property
    def configured(self) -> bool:
        return bool(self.key)

    def close(self) -> None:
        if self.owns_client:
            self.client.close()

    def publish(self, title: str, markdown: str) -> PublishedGist:
        encoded = markdown.encode("utf-8")
        if (
            not title.strip()
            or not markdown.startswith("# ")
            or not markdown.splitlines()[0][2:].strip()
            or len(encoded) > MAX_REPORT_BYTES
        ):
            raise GistError("report is not valid publishable Markdown")
        if not self.key:
            raise GistError("Wavey Gist credential is not configured")
        digest = hashlib.sha256(encoded).hexdigest()
        snapshot_digest = _snapshot_sha256(title, digest)
        try:
            response = self.client.post(
                f"{API_BASE}/gists",
                headers={
                    "Authorization": f"Bearer {self.key}",
                    "Content-Type": "application/json",
                    "User-Agent": "govlens/0.1",
                },
                json={
                    "title": title,
                    "files": {REPORT_FILENAME: {"content": markdown}},
                },
            )
        except httpx.HTTPError as exc:
            raise GistUnknown("Wavey Gist publication response was lost") from exc
        if response.status_code == 408 or response.status_code >= 500:
            raise GistUnknown("Wavey Gist publication outcome is unclear")
        if response.status_code >= 400:
            raise GistError("Wavey Gist rejected the report")
        if response.status_code not in {200, 201}:
            raise GistUnknown("Wavey Gist returned an unexpected publication response")
        try:
            body = response.json()
        except ValueError as exc:
            raise GistUnknown("Wavey Gist returned malformed publication data") from exc
        if not isinstance(body, dict):
            raise GistUnknown("Wavey Gist returned malformed publication data")
        gist_id = body.get("id")
        revision = body.get("revision_number")
        latest_revision = body.get("latest_revision_number")
        url = body.get("url")
        candidate = url if isinstance(url, str) else None
        if (
            not isinstance(gist_id, str)
            or not GIST_ID.fullmatch(gist_id)
            or candidate != f"{PUBLIC_BASE}/{gist_id}"
            or not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision < 1
            or not isinstance(latest_revision, int)
            or isinstance(latest_revision, bool)
            or latest_revision < revision
            or not _matches_snapshot(
                body,
                title=title,
                markdown=markdown,
                content_sha256=digest,
                snapshot_sha256=snapshot_digest,
            )
        ):
            raise GistUnknown("Wavey Gist publication data could not be verified", candidate)

        revision_url = f"{candidate}/revisions/{revision}"
        try:
            raw = self.client.get(f"{revision_url}/raw", headers={"User-Agent": "govlens/0.1"})
            render = self.client.get(
                f"{API_BASE}/gists/{gist_id}/revisions/{revision}/render",
                headers={"User-Agent": "govlens/0.1"},
            )
            public = self.client.get(revision_url, headers={"User-Agent": "govlens/0.1"})
            render_body = render.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise GistUnknown("Wavey Gist verification response was invalid", candidate) from exc
        if raw.status_code != 200 or raw.content != encoded:
            raise GistUnknown("Wavey Gist raw revision did not match", candidate)
        if (
            render.status_code != 200
            or not _matches_snapshot(
                render_body,
                title=title,
                markdown=markdown,
                content_sha256=digest,
                snapshot_sha256=snapshot_digest,
            )
            or render_body.get("revision_number") != revision
        ):
            raise GistUnknown("Wavey Gist render revision did not match", candidate)
        if public.status_code != 200 or public.has_redirect_location:
            raise GistUnknown("Wavey Gist public revision was unavailable", candidate)
        return PublishedGist(url=validate_gist_url(candidate), revision=revision, sha256=digest)
