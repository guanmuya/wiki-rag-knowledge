"""Fail-closed checks for a tagged Windows desktop release."""

import ast
import base64
import hashlib
import re
from pathlib import Path

import yaml


class ReleaseError(ValueError):
    """A release must not proceed because its inputs are inconsistent."""


_TAG = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
_HEADING = re.compile(r"^ {0,3}##[ \t]+(.+?)\s*$")
_PUBLIC_HEADING = re.compile(r"^1\.[ \t]+Release[ \t]+正文草稿(?:[ \t（(]|$)")
_NUMBERED_HEADING = re.compile(r"^ {0,3}##[ \t]+([0-9]+)\.(?:[ \t]|$)")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})([^`]*)$")


def parse_tag(tag):
    if not isinstance(tag, str) or not _TAG.fullmatch(tag):
        raise ReleaseError("Release tag must have the form vMAJOR.MINOR.PATCH")
    return tuple(int(part) for part in _TAG.fullmatch(tag).groups())


def _backend_version(source):
    try:
        tree = ast.parse(source)
    except (SyntaxError, TypeError) as exc:
        raise ReleaseError("Backend version source cannot be parsed") from exc
    values = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "VERSION" for t in node.targets):
            values.append(node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "VERSION":
            values.append(node.value)
    if len(values) != 1 or not isinstance(values[0], ast.Constant) or not isinstance(values[0].value, str):
        raise ReleaseError("Backend must define one literal VERSION")
    return values[0].value


def check_release_version(tag, frontend_version, backend_source, releases):
    version = parse_tag(tag)
    expected = tag[1:]
    if frontend_version != expected or _backend_version(backend_source) != expected:
        raise ReleaseError("Tag, frontend version and backend VERSION must match")
    for release in releases:
        if not isinstance(release, dict) or type(release.get("draft")) is not bool or type(release.get("prerelease")) is not bool:
            raise ReleaseError("Cannot determine the status of an existing release")
        if release["draft"] or release["prerelease"]:
            continue
        old_tag = release.get("tag_name")
        if not isinstance(old_tag, str):
            raise ReleaseError("Existing published release has no tag")
        try:
            old_version = parse_tag(old_tag)
        except ReleaseError:
            continue
        if old_version >= version:
            raise ReleaseError("Release version must exceed every published stable version")


def extract_notes(text):
    if not isinstance(text, str):
        raise ReleaseError("Release notes must be text")
    in_section = False
    heading_count = 0
    fence = None
    capture = None
    blocks = []
    next_section_count = 0
    for line in text.splitlines():
        match = _FENCE.match(line)
        if fence is not None:
            if capture is not None and _NUMBERED_HEADING.match(line):
                raise ReleaseError("Public markdown block crosses a numbered document section")
            if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= fence[1] and not match.group(2).strip():
                if capture is not None:
                    blocks.append("\n".join(capture).strip())
                fence = None
                capture = None
            elif capture is not None:
                capture.append(line)
            continue
        if match:
            fence = (match.group(1)[0], len(match.group(1)))
            if in_section and match.group(2).strip().lower() == "markdown":
                capture = []
            continue
        heading = _HEADING.match(line)
        if heading:
            numbered = _NUMBERED_HEADING.match(line)
            if numbered and numbered.group(1) == "2":
                if not blocks:
                    raise ReleaseError("Internal section must follow the closed public block")
                next_section_count += 1
            in_section = bool(_PUBLIC_HEADING.match(heading.group(1)))
            if in_section:
                heading_count += 1
    if heading_count != 1 or len(blocks) != 1 or not blocks[0] or capture is not None or next_section_count != 1:
        raise ReleaseError("Release notes need one closed public markdown block in section 1, followed by section 2")
    return blocks[0]


class _UniqueKeyLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        mapping = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in mapping
            except TypeError as exc:
                raise ReleaseError("Invalid update manifest key") from exc
            if duplicate:
                raise ReleaseError("Duplicate key in update manifest")
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def _hash_file(path):
    sha512 = hashlib.sha512()
    sha256 = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            sha512.update(chunk)
            sha256.update(chunk)
    return size, base64.b64encode(sha512.digest()).decode("ascii"), sha256.hexdigest()


def verify_assets(version, directory):
    parse_tag("v" + version if isinstance(version, str) else version)
    directory = Path(directory)
    installer = f"wiki-rag-knowledge-{version}-x64-setup.exe"
    names = ("latest.yml", installer, installer + ".blockmap")
    paths = {name: directory / name for name in names}
    for name, path in paths.items():
        if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
            raise ReleaseError(f"Missing, empty or unsafe release asset: {name}")
    if paths["latest.yml"].stat().st_size > 1024 * 1024:
        raise ReleaseError("Update manifest is unexpectedly large")
    try:
        manifest = yaml.load(paths["latest.yml"].read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ReleaseError("Could not read update manifest") from exc
    if not isinstance(manifest, dict) or manifest.get("version") != version or manifest.get("path") != installer:
        raise ReleaseError("Update manifest version or installer path does not match")
    files = manifest.get("files")
    if not isinstance(files, list) or len(files) != 1 or not isinstance(files[0], dict):
        raise ReleaseError("Update manifest must name exactly one installer")
    size, sha512, installer_sha256 = _hash_file(paths[installer])
    entry = files[0]
    if (entry.get("url") != installer or type(entry.get("size")) is not int or entry["size"] != size
            or entry.get("sha512") != sha512 or manifest.get("sha512") != sha512):
        raise ReleaseError("Update manifest does not match the installer bytes")
    hashes = {installer: installer_sha256}
    for name in ("latest.yml", installer + ".blockmap"):
        hashes[name] = _hash_file(paths[name])[2]
    return {"version": version, "installer": installer, "sha512": sha512, "sha256": hashes}
