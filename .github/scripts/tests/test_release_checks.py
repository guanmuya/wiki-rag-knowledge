import base64
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import release_checks as checks


class TagTests(unittest.TestCase):
    def test_accepts_stable_tag_and_compares_numbers(self):
        self.assertEqual(checks.parse_tag("v1.10.3"), (1, 10, 3))
        self.assertGreater(checks.parse_tag("v1.10.3"), checks.parse_tag("v1.9.99"))

    def test_rejects_invalid_and_prerelease_tags(self):
        for tag in ("1.0.23", "v1.0", "v01.0.23", "v1.0.23-rc1", "v1.0.23+build", "v1.0.23\n"):
            with self.subTest(tag=tag), self.assertRaises(checks.ReleaseError):
                checks.parse_tag(tag)

    def test_checks_both_source_versions_and_published_stable_releases(self):
        releases = [
            {"tag_name": "v1.0.22", "draft": False, "prerelease": False},
            {"tag_name": "v9.0.0", "draft": True, "prerelease": False},
            {"tag_name": "v10.0.0", "draft": False, "prerelease": True},
        ]
        checks.check_release_version("v1.0.23", "1.0.23", 'VERSION = "1.0.23"', releases)
        with self.assertRaises(checks.ReleaseError):
            checks.check_release_version("v1.0.23", "1.0.22", 'VERSION = "1.0.23"', releases)
        with self.assertRaises(checks.ReleaseError):
            checks.check_release_version("v1.0.23", "1.0.23", 'VERSION = "1.0.22"', releases)
        with self.assertRaises(checks.ReleaseError):
            checks.check_release_version("v1.0.22", "1.0.22", 'VERSION = "1.0.22"', releases)

    def test_rejects_ambiguous_backend_version(self):
        source = 'VERSION = "1.0.23"\nVERSION = "1.0.22"\n'
        with self.assertRaises(checks.ReleaseError):
            checks.check_release_version("v1.0.23", "1.0.23", source, [])


class ReleaseNotesTests(unittest.TestCase):
    def test_extracts_only_public_section_and_keeps_nested_heading(self):
        text = (
            "# 内部发布说明\n\n## 1. Release 正文草稿（可直接粘贴）\n\n"
            "```markdown\n## 企业知识库 v1.0.23\n\n用户可见内容\n```\n\n"
            "## 2. 内部步骤\n\n不要公开的操作说明\n"
        )
        self.assertEqual(checks.extract_notes(text), "## 企业知识库 v1.0.23\n\n用户可见内容")
        self.assertEqual(checks.extract_notes(text.replace("\n", "\r\n")), "## 企业知识库 v1.0.23\n\n用户可见内容")

    def test_rejects_unclosed_public_fence_before_internal_steps(self):
        notes = (Path(__file__).resolve().parents[3] / "docs" / "RELEASE_NOTES_v1.0.22.md").read_text(encoding="utf-8")
        public_start = notes.index("```markdown")
        public_end = notes.index("\n```\n", public_start)
        broken = notes[:public_end] + notes[public_end:].replace("\n```\n", "\n", 1)
        with self.assertRaises(checks.ReleaseError):
            checks.extract_notes(broken)

    def test_rejects_numbered_top_level_section_inside_public_block(self):
        text = "## 1. Release 正文草稿\n```markdown\n## 2. 内部步骤\ncommands\n```\n## 2. 内部步骤\n"
        with self.assertRaises(checks.ReleaseError):
            checks.extract_notes(text)

    def test_rejects_missing_empty_or_ambiguous_public_content(self):
        cases = (
            "## 1. Release 正文草稿\n\n没有代码块",
            "## 1. Release 正文草稿\n```markdown\n   \n```",
            "## 1. Release 正文草稿\n```markdown\n公开正文\n```",
            "## 1. Release 正文草稿\n```markdown\nA\n```\n```markdown\nB\n```",
            "## 1. Release 正文草稿\n```markdown\nA\n```\n## 1. Release 正文草稿\n```markdown\nB\n```",
        )
        for text in cases:
            with self.subTest(text=text), self.assertRaises(checks.ReleaseError):
                checks.extract_notes(text)


class AssetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.name = "wiki-rag-knowledge-1.0.23-x64-setup.exe"
        self.installer = self.directory / self.name
        self.installer.write_bytes(b"test installer payload")
        (self.directory / (self.name + ".blockmap")).write_bytes(b"test blockmap")
        self.digest = base64.b64encode(hashlib.sha512(b"test installer payload").digest()).decode("ascii")
        self.manifest = (
            'version: "1.0.23"\nfiles:\n'
            f'  - url: {self.name}\n    sha512: {self.digest}\n    size: 22\n'
            f'path: {self.name}\nsha512: {self.digest}\n'
        )
        (self.directory / "latest.yml").write_text(self.manifest, encoding="utf-8")

    def test_accepts_matching_three_assets(self):
        result = checks.verify_assets("1.0.23", self.directory)
        self.assertEqual(result["version"], "1.0.23")
        self.assertEqual(result["installer"], self.name)

    def test_rejects_missing_or_empty_blockmap(self):
        blockmap = self.directory / (self.name + ".blockmap")
        blockmap.write_bytes(b"")
        with self.assertRaises(checks.ReleaseError):
            checks.verify_assets("1.0.23", self.directory)
        blockmap.unlink()
        with self.assertRaises(checks.ReleaseError):
            checks.verify_assets("1.0.23", self.directory)

    def test_rejects_changed_installer_after_manifest_was_built(self):
        self.installer.write_bytes(b"changed installer data")
        with self.assertRaises(checks.ReleaseError):
            checks.verify_assets("1.0.23", self.directory)

    def test_rejects_wrong_manifest_version_path_size_or_hash(self):
        mutations = (
            self.manifest.replace('version: "1.0.23"', 'version: "1.0.24"'),
            self.manifest.replace(f'path: {self.name}', 'path: ../outside.exe'),
            self.manifest.replace('size: 22', 'size: 23'),
            self.manifest.replace(self.digest, "bad-digest", 1),
            self.manifest + 'version: "1.0.23"\n',
        )
        for manifest in mutations:
            with self.subTest(manifest=manifest), self.assertRaises(checks.ReleaseError):
                (self.directory / "latest.yml").write_text(manifest, encoding="utf-8")
                checks.verify_assets("1.0.23", self.directory)


if __name__ == "__main__":
    unittest.main()
