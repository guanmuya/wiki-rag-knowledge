import base64
import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from release_checks import ReleaseError
import release_publish as publish


SHA = "a" * 40
TAG = "v1.0.23"
REPO = "guanmuya/wiki-rag-knowledge"
RUN_ID = "987654"


class FakeGit:
    def __init__(self):
        self.head = SHA
        self.remote_tag = SHA
        self.ancestor = True

    def head_sha(self):
        return self.head

    def tag_sha(self, tag):
        return self.remote_tag

    def is_master_ancestor(self, sha):
        return self.ancestor


class FakeGitHub:
    def __init__(self):
        self.releases = [{"tag_name": "v1.0.22", "draft": False, "prerelease": False, "body": "old", "assets": []}]
        self.files = {}
        self.fail_upload = False
        self.corrupt_download = False
        self.before_publish_check = None
        self.deleted_assets = []

    def list_releases(self):
        return copy.deepcopy(self.releases)

    def create_draft(self, tag, body):
        self.releases.append({"tag_name": tag, "draft": True, "prerelease": False, "body": body, "assets": []})
        self.files[tag] = {}

    def upload(self, tag, paths):
        if self.fail_upload:
            raise ReleaseError("upload failed")
        release = next(r for r in self.releases if r["tag_name"] == tag)
        for path in paths:
            path = Path(path)
            if path.name in self.files[tag]:
                raise ReleaseError("would overwrite existing asset")
            data = path.read_bytes()
            self.files[tag][path.name] = data
            release["assets"].append({"name": path.name, "size": len(data), "state": "uploaded"})

    def delete_asset(self, asset_id):
        for release in self.releases:
            for asset in release["assets"]:
                if asset.get("id") == asset_id:
                    self.deleted_assets.append(asset_id)
                    release["assets"].remove(asset)
                    self.files[release["tag_name"]].pop(asset["name"], None)
                    return
        raise ReleaseError("unknown asset")

    def download(self, tag, directory):
        for name, data in self.files[tag].items():
            if self.corrupt_download and name.endswith(".exe"):
                data = b"corrupt download"
            (Path(directory) / name).write_bytes(data)
        if self.before_publish_check is not None:
            hook, self.before_publish_check = self.before_publish_check, None
            hook()

    def publish(self, tag, body):
        release = next(r for r in self.releases if r["tag_name"] == tag)
        release["body"] = body
        release["draft"] = False


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "frontend").mkdir()
        (self.root / "backend" / "app").mkdir(parents=True)
        (self.root / "docs").mkdir()
        (self.root / "frontend" / "package.json").write_text(json.dumps({"version": "1.0.23"}), encoding="utf-8")
        (self.root / "backend" / "app" / "main.py").write_text('VERSION = "1.0.23"\n', encoding="utf-8")
        (self.root / "docs" / f"RELEASE_NOTES_{TAG}.md").write_text(
            "## 1. Release 正文草稿\n```markdown\n## 公开说明\n```\n## 2. 内部步骤\nsecret instructions\n",
            encoding="utf-8",
        )
        self.assets = self.root / "assets"
        self.assets.mkdir()
        name = "wiki-rag-knowledge-1.0.23-x64-setup.exe"
        payload = b"release installer"
        digest = base64.b64encode(hashlib.sha512(payload).digest()).decode("ascii")
        (self.assets / name).write_bytes(payload)
        (self.assets / (name + ".blockmap")).write_bytes(b"blockmap")
        (self.assets / "latest.yml").write_text(
            f'version: "1.0.23"\nfiles:\n  - url: {name}\n    sha512: {digest}\n    size: {len(payload)}\n'
            f'path: {name}\nsha512: {digest}\n', encoding="utf-8",
        )
        self.git = FakeGit()
        self.github = FakeGitHub()

    def context(self):
        return publish.preflight(TAG, SHA, REPO, RUN_ID, self.root, self.git, self.github)

    def release(self):
        return next((r for r in self.github.releases if r["tag_name"] == TAG), None)

    def test_manual_ci_preview_checks_current_version_without_releasing(self):
        self.assertEqual(publish.preview_version(self.root), "1.0.23")
        self.assertIsNone(self.release())
        (self.root / "backend" / "app" / "main.py").write_text('VERSION = "1.0.22"\n', encoding="utf-8")
        with self.assertRaises(ReleaseError):
            publish.preview_version(self.root)
        self.assertIsNone(self.release())

    def test_manual_ci_can_validate_assets_but_cannot_publish(self):
        script = Path(publish.__file__)
        env = {**os.environ, "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF_TYPE": "branch", "RELEASE_VERSION": "1.0.23"}
        verify = subprocess.run([sys.executable, str(script), "verify-assets", "--assets-dir", str(self.assets)], env=env, capture_output=True, text=True)
        self.assertEqual(verify.returncode, 0, verify.stderr)
        attempt = subprocess.run([sys.executable, str(script), "publish", "--assets-dir", str(self.assets)], env=env, capture_output=True, text=True)
        self.assertNotEqual(attempt.returncode, 0)
        self.assertIn("tag push", attempt.stderr)

    def test_publishes_only_verified_assets_with_public_notes(self):
        publish.publish(self.context(), self.assets, self.git, self.github)
        release = self.release()
        self.assertFalse(release["draft"])
        self.assertEqual(release["body"], "## 公开说明")
        self.assertEqual(len(release["assets"]), 3)
        self.assertNotIn("secret instructions", release["body"])

    def test_non_master_or_moved_tag_fails_before_draft_creation(self):
        self.git.ancestor = False
        with self.assertRaises(ReleaseError):
            self.context()
        self.assertIsNone(self.release())
        self.git.ancestor = True
        self.git.remote_tag = "b" * 40
        with self.assertRaises(ReleaseError):
            self.context()
        self.assertIsNone(self.release())

    def test_upload_failure_leaves_a_draft_not_a_public_release(self):
        context = self.context()
        self.github.fail_upload = True
        with self.assertRaises(ReleaseError):
            publish.publish(context, self.assets, self.git, self.github)
        self.assertTrue(self.release()["draft"])

    def test_corrupt_download_does_not_publish(self):
        context = self.context()
        self.github.corrupt_download = True
        with self.assertRaises(ReleaseError):
            publish.publish(context, self.assets, self.git, self.github)
        self.assertTrue(self.release()["draft"])

    def test_unrelated_draft_is_not_modified(self):
        context = self.context()
        draft = {"tag_name": TAG, "draft": True, "prerelease": False, "body": "someone else's work", "assets": [
            {"id": 42, "name": "latest.yml", "size": 0, "state": "starter"}
        ]}
        self.github.releases.append(copy.deepcopy(draft))
        with self.assertRaises(ReleaseError):
            publish.publish(context, self.assets, self.git, self.github)
        self.assertEqual(self.release(), draft)
        self.assertEqual(self.github.deleted_assets, [])

    def test_published_release_is_never_replaced(self):
        context = self.context()
        existing = {"tag_name": TAG, "draft": False, "prerelease": False, "body": "published", "assets": []}
        self.github.releases.append(copy.deepcopy(existing))
        with self.assertRaises(ReleaseError):
            publish.publish(context, self.assets, self.git, self.github)
        self.assertEqual(self.release(), existing)

    def test_owned_partial_draft_can_resume_without_overwriting(self):
        context = self.context()
        self.github.fail_upload = True
        with self.assertRaises(ReleaseError):
            publish.publish(context, self.assets, self.git, self.github)
        self.github.fail_upload = False
        self.github.upload(TAG, [self.assets / "latest.yml"])
        publish.publish(context, self.assets, self.git, self.github)
        self.assertFalse(self.release()["draft"])
        self.assertEqual(len(self.release()["assets"]), 3)

    def test_owned_draft_recovers_from_zero_byte_starter_asset(self):
        context = self.context()
        self.github.create_draft(TAG, publish._draft_body(context))
        self.github.upload(TAG, [self.assets / "latest.yml"])
        self.release()["assets"].append({"id": 42, "name": "wiki-rag-knowledge-1.0.23-x64-setup.exe", "size": 0, "state": "starter"})
        publish.publish(context, self.assets, self.git, self.github)
        self.assertEqual(self.github.deleted_assets, [42])
        self.assertFalse(self.release()["draft"])
        self.assertEqual(len(self.release()["assets"]), 3)

    def test_does_not_remove_unexpected_or_nonzero_starter_assets(self):
        for asset in (
            {"id": 42, "name": "unrelated.exe", "size": 0, "state": "starter"},
            {"id": 42, "name": "latest.yml", "size": 1, "state": "starter"},
        ):
            with self.subTest(asset=asset):
                github = FakeGitHub()
                context = publish.preflight(TAG, SHA, REPO, RUN_ID, self.root, self.git, github)
                github.create_draft(TAG, publish._draft_body(context))
                github.releases[-1]["assets"].append(asset)
                with self.assertRaises(ReleaseError):
                    publish.publish(context, self.assets, self.git, github)
                self.assertEqual(github.deleted_assets, [])

    def test_newer_release_before_publication_stops_old_version(self):
        context = self.context()
        self.github.before_publish_check = lambda: self.github.releases.append(
            {"tag_name": "v1.0.24", "draft": False, "prerelease": False, "body": "new", "assets": []}
        )
        with self.assertRaises(ReleaseError):
            publish.publish(context, self.assets, self.git, self.github)
        self.assertTrue(self.release()["draft"])


class GitOperationsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "work"
        self.origin = self.root / "origin.git"
        subprocess.run(["git", "init", "--bare", str(self.origin)], check=True, capture_output=True)
        subprocess.run(["git", "init", "-b", "master", str(self.repo)], check=True, capture_output=True)
        self.git("config", "user.name", "Release Test")
        self.git("config", "user.email", "release-test@example.invalid")
        (self.repo / "file.txt").write_text("master", encoding="utf-8")
        self.git("add", "file.txt")
        self.git("commit", "-m", "base")
        self.git("remote", "add", "origin", str(self.origin))
        self.git("push", "origin", "master")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True, text=True).stdout.strip()

    def test_reads_lightweight_and_annotated_remote_tags_and_checks_ancestry(self):
        self.git("tag", "v1.0.23")
        self.git("tag", "-a", "v1.0.24", "-m", "v1.0.24")
        self.git("push", "origin", "v1.0.23", "v1.0.24")
        ops = publish.GitOperations(self.repo)
        head = self.git("rev-parse", "HEAD")
        self.assertEqual(ops.head_sha(), head)
        self.assertEqual(ops.tag_sha("v1.0.23"), head)
        self.assertEqual(ops.tag_sha("v1.0.24"), head)
        self.assertTrue(ops.is_master_ancestor(head))

        self.git("switch", "-c", "unmerged")
        (self.repo / "file.txt").write_text("not merged", encoding="utf-8")
        self.git("commit", "-am", "unmerged")
        self.git("tag", "v1.0.25")
        self.git("push", "origin", "v1.0.25")
        unmerged = self.git("rev-parse", "HEAD")
        self.assertEqual(ops.tag_sha("v1.0.25"), unmerged)
        self.assertFalse(ops.is_master_ancestor(unmerged))


if __name__ == "__main__":
    unittest.main()
