"""Prepare, verify, and automatically publish a tagged desktop release."""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from release_checks import ReleaseError, check_release_version, extract_notes, parse_tag, verify_assets


_SHA = re.compile(r"[0-9a-f]{40}")
_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


@dataclass(frozen=True)
class ReleaseContext:
    tag: str
    sha: str
    repo: str
    run_id: str
    version: str
    notes: str
    frontend_version: str
    backend_source: str


def _check_git(context, git):
    if git.head_sha() != context.sha or git.tag_sha(context.tag) != context.sha:
        raise ReleaseError("Checked-out commit or remote tag changed")
    if not git.is_master_ancestor(context.sha):
        raise ReleaseError("Release commit is not in remote master history")


def preflight(tag, sha, repo, run_id, root, git, github):
    parse_tag(tag)
    if not isinstance(sha, str) or not _SHA.fullmatch(sha):
        raise ReleaseError("Release commit must be a full Git SHA")
    if not isinstance(repo, str) or not _REPOSITORY.fullmatch(repo):
        raise ReleaseError("Invalid repository name")
    if not isinstance(run_id, str) or not run_id.isascii() or not run_id.isdigit():
        raise ReleaseError("Invalid workflow run ID")
    root = Path(root)
    try:
        frontend = json.loads((root / "frontend" / "package.json").read_text(encoding="utf-8"))
        backend = (root / "backend" / "app" / "main.py").read_text(encoding="utf-8")
        notes_text = (root / "docs" / f"RELEASE_NOTES_{tag}.md").read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("Could not read version files or versioned release notes") from exc
    if not isinstance(frontend, dict):
        raise ReleaseError("Invalid frontend package metadata")
    context = ReleaseContext(tag, sha, repo, run_id, tag[1:], extract_notes(notes_text), frontend.get("version"), backend)
    _check_git(context, git)
    check_release_version(tag, context.frontend_version, backend, github.list_releases())
    return context


def preview_version(root):
    """Validate the checked-out source for a manual, non-publishing CI run."""
    root = Path(root)
    try:
        frontend = json.loads((root / "frontend" / "package.json").read_text(encoding="utf-8"))
        backend = (root / "backend" / "app" / "main.py").read_text(encoding="utf-8")
        version = frontend["version"]
        if not isinstance(version, str):
            raise ReleaseError("Invalid frontend version")
        tag = "v" + version
        parse_tag(tag)
        notes = (root / "docs" / f"RELEASE_NOTES_{tag}.md").read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ReleaseError("Could not read version files or versioned release notes") from exc
    check_release_version(tag, version, backend, [])
    extract_notes(notes)
    return version


def _write_version(version):
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as stream:
            stream.write(f"version={version}\n")


def _draft_body(context):
    return context.notes + f"\n\n<!-- automated-release tag={context.tag} sha={context.sha} run={context.run_id} -->"


def _find_owned_draft(context, releases):
    matches = [item for item in releases if item.get("tag_name") == context.tag]
    if len(matches) > 1:
        raise ReleaseError("Multiple releases found for the same tag")
    if not matches:
        return None
    release = matches[0]
    if release.get("draft") is not True or release.get("prerelease") is not False:
        raise ReleaseError("An existing non-draft release cannot be overwritten")
    if release.get("body") != _draft_body(context):
        raise ReleaseError("Existing draft is not owned by this workflow run")
    return release


def _asset_names(context):
    installer = f"wiki-rag-knowledge-{context.version}-x64-setup.exe"
    return ("latest.yml", installer, installer + ".blockmap")


def _classify_draft_assets(context, release, asset_dir):
    expected = set(_asset_names(context))
    assets = release.get("assets")
    if not isinstance(assets, list):
        raise ReleaseError("Draft asset list is unavailable")
    seen = set()
    uploaded = set()
    interrupted = {}
    for asset in assets:
        if not isinstance(asset, dict):
            raise ReleaseError("Invalid draft asset metadata")
        name = asset.get("name")
        if name not in expected or name in seen:
            raise ReleaseError("Draft contains an unexpected or duplicate asset")
        seen.add(name)
        if asset.get("state") == "uploaded":
            if type(asset.get("size")) is not int or asset["size"] != (asset_dir / name).stat().st_size:
                raise ReleaseError("Draft asset size does not match the build")
            uploaded.add(name)
        elif asset.get("state") == "starter" and asset.get("size") == 0 and type(asset.get("size")) is int:
            asset_id = asset.get("id")
            if type(asset_id) is not int or asset_id <= 0 or asset_id in interrupted.values():
                raise ReleaseError("Interrupted draft asset has an invalid identifier")
            interrupted[name] = asset_id
        else:
            raise ReleaseError("Draft contains an incomplete or invalid asset")
    return uploaded, interrupted


def _check_draft_assets(context, release, asset_dir, require_all):
    uploaded, interrupted = _classify_draft_assets(context, release, asset_dir)
    if interrupted:
        raise ReleaseError("Draft contains an interrupted upload")
    if require_all and uploaded != set(_asset_names(context)):
        raise ReleaseError("Draft is missing a required release asset")
    return uploaded


def _verify_downloaded_subset(context, asset_dir, names, expected_hashes, github):
    import hashlib

    if not names:
        return
    with tempfile.TemporaryDirectory(prefix="release-download-") as temp:
        destination = Path(temp)
        github.download(context.tag, destination)
        actual_names = {path.name for path in destination.iterdir()}
        if actual_names != set(names):
            raise ReleaseError("Downloaded draft assets do not match their metadata")
        for name in names:
            path = destination / name
            if path.is_symlink() or not path.is_file():
                raise ReleaseError("Downloaded release asset is unsafe")
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != expected_hashes[name]:
                raise ReleaseError("Downloaded draft asset does not match the verified build")
        if set(names) == set(_asset_names(context)):
            verify_assets(context.version, destination)


def publish(context, assets, git, github):
    asset_dir = Path(assets)
    verified = verify_assets(context.version, asset_dir)
    _check_git(context, git)
    releases = github.list_releases()
    check_release_version(context.tag, context.frontend_version, context.backend_source, releases)
    draft = _find_owned_draft(context, releases)
    if draft is None:
        github.create_draft(context.tag, _draft_body(context))
        draft = _find_owned_draft(context, github.list_releases())
        if draft is None:
            raise ReleaseError("New release draft was not found")
    _, interrupted = _classify_draft_assets(context, draft, asset_dir)
    for name, asset_id in interrupted.items():
        # GitHub can leave an empty 'starter' asset after an upload error. Only
        # remove it after confirming the draft and that same asset still match.
        draft = _find_owned_draft(context, github.list_releases())
        if draft is None:
            raise ReleaseError("Release draft disappeared before upload recovery")
        _, current = _classify_draft_assets(context, draft, asset_dir)
        if current.get(name) != asset_id:
            raise ReleaseError("Interrupted draft asset changed before upload recovery")
        github.delete_asset(asset_id)
    draft = _find_owned_draft(context, github.list_releases())
    if draft is None:
        raise ReleaseError("Release draft disappeared before uploading")
    existing = _check_draft_assets(context, draft, asset_dir, require_all=False)
    _verify_downloaded_subset(context, asset_dir, existing, verified["sha256"], github)
    missing = [asset_dir / name for name in _asset_names(context) if name not in existing]
    if missing:
        github.upload(context.tag, missing)
    draft = _find_owned_draft(context, github.list_releases())
    if draft is None:
        raise ReleaseError("Release draft disappeared")
    names = _check_draft_assets(context, draft, asset_dir, require_all=True)
    _verify_downloaded_subset(context, asset_dir, names, verified["sha256"], github)
    _check_git(context, git)
    releases = github.list_releases()
    check_release_version(context.tag, context.frontend_version, context.backend_source, releases)
    draft = _find_owned_draft(context, releases)
    if draft is None:
        raise ReleaseError("Release draft disappeared before publication")
    _check_draft_assets(context, draft, asset_dir, require_all=True)
    github.publish(context.tag, context.notes)


def _run(command, cwd=None):
    try:
        result = subprocess.run(command, cwd=cwd, text=True, encoding="utf-8", errors="replace", capture_output=True, check=False)
    except OSError as exc:
        raise ReleaseError(f"Could not run {command[0]}") from exc
    if result.returncode:
        error = result.stderr.strip()
        token = os.environ.get("GH_TOKEN")
        if token:
            error = error.replace(token, "[redacted]")
        raise ReleaseError(f"{command[0]} failed: {error[:500]}")
    return result.stdout


class GitOperations:
    def __init__(self, root):
        self.root = Path(root)

    def head_sha(self):
        return _run(["git", "rev-parse", "HEAD^{commit}"], self.root).strip()

    def tag_sha(self, tag):
        parse_tag(tag)
        ref = f"refs/tags/{tag}"
        output = _run(["git", "ls-remote", "origin", ref, ref + "^{}"], self.root)
        refs = {}
        for line in output.splitlines():
            parts = line.split("\t")
            if len(parts) != 2 or parts[1] not in (ref, ref + "^{}") or parts[1] in refs:
                raise ReleaseError("Unexpected remote tag response")
            refs[parts[1]] = parts[0]
        sha = refs.get(ref + "^{}", refs.get(ref))
        if not sha or not _SHA.fullmatch(sha):
            raise ReleaseError("Remote release tag does not exist")
        return sha

    def is_master_ancestor(self, sha):
        _run(["git", "fetch", "--no-tags", "origin", "+refs/heads/master:refs/remotes/origin/master"], self.root)
        result = subprocess.run(["git", "merge-base", "--is-ancestor", sha, "refs/remotes/origin/master"], cwd=self.root, capture_output=True, check=False)
        if result.returncode not in (0, 1):
            raise ReleaseError("Unable to verify release commit ancestry")
        return result.returncode == 0


class GitHubCLI:
    def __init__(self, repo):
        if not _REPOSITORY.fullmatch(repo):
            raise ReleaseError("Invalid repository name")
        self.repo = repo

    def list_releases(self):
        output = _run(["gh", "api", "--paginate", "--slurp", f"repos/{self.repo}/releases?per_page=100"])
        try:
            pages = json.loads(output)
        except json.JSONDecodeError as exc:
            raise ReleaseError("Could not parse GitHub releases") from exc
        if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
            raise ReleaseError("Unexpected GitHub release response")
        return [release for page in pages for release in page]

    def create_draft(self, tag, body):
        with tempfile.TemporaryDirectory(prefix="release-notes-") as temp:
            notes = Path(temp) / "notes.md"
            notes.write_text(body, encoding="utf-8")
            _run(["gh", "release", "create", tag, "--repo", self.repo, "--verify-tag", "--draft", "--title", tag, "--notes-file", str(notes)])

    def upload(self, tag, paths):
        _run(["gh", "release", "upload", tag, "--repo", self.repo, *[str(path) for path in paths]])

    def delete_asset(self, asset_id):
        if type(asset_id) is not int or asset_id <= 0:
            raise ReleaseError("Invalid release asset identifier")
        _run(["gh", "api", "--method", "DELETE", f"repos/{self.repo}/releases/assets/{asset_id}"])

    def download(self, tag, directory):
        _run(["gh", "release", "download", tag, "--repo", self.repo, "--dir", str(directory), "--pattern", "*"])

    def publish(self, tag, body):
        with tempfile.TemporaryDirectory(prefix="release-notes-") as temp:
            notes = Path(temp) / "notes.md"
            notes.write_text(body, encoding="utf-8")
            _run(["gh", "release", "edit", tag, "--repo", self.repo, "--draft=false", "--latest", "--notes-file", str(notes)])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preflight", "preview", "verify-assets", "publish"))
    parser.add_argument("--assets-dir", default="frontend/release/updates")
    args = parser.parse_args()
    event = os.environ.get("GITHUB_EVENT_NAME")
    tag_push = event == "push" and os.environ.get("GITHUB_REF_TYPE") == "tag"
    if args.action == "preview":
        if event != "workflow_dispatch":
            raise ReleaseError("Preview requires a manual CI event")
        version = preview_version(Path.cwd())
        _write_version(version)
        print(f"Non-publishing CI preview for {version}")
        return
    if args.action == "verify-assets" and event == "workflow_dispatch":
        version = os.environ.get("RELEASE_VERSION", "")
        parse_tag("v" + version)
        verify_assets(version, args.assets_dir)
        print("Release assets validated")
        return
    if not tag_push:
        raise ReleaseError("Release commands require a tag push event")
    tag = os.environ.get("GITHUB_REF_NAME", "")
    parse_tag(tag)
    if args.action == "verify-assets":
        if os.environ.get("RELEASE_VERSION", tag[1:]) != tag[1:]:
            raise ReleaseError("Release version does not match the tag")
        verify_assets(tag[1:], args.assets_dir)
        print("Release assets validated")
        return
    root = Path.cwd()
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    git = GitOperations(root)
    github = GitHubCLI(repo)
    context = preflight(tag, os.environ.get("GITHUB_SHA", ""), repo, os.environ.get("GITHUB_RUN_ID", ""), root, git, github)
    if args.action == "publish":
        publish(context, args.assets_dir, git, github)
        print(f"Published {tag}")
    else:
        _write_version(context.version)
        print(f"Preflight passed for {tag}")


if __name__ == "__main__":
    try:
        main()
    except ReleaseError as exc:
        print(f"Release blocked: {exc}", file=sys.stderr)
        sys.exit(1)
