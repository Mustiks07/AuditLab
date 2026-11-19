"""Prevent a three-image Docker archive with an incomplete containerd OCI index."""
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest

from tools.build_release import validate_image_archive


class ImageArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "images.tar"
        self.images = {role: dict(id="sha256:" + digit * 64, tag="auditlab-" + role + ":test")
                       for role, digit in zip(("runtime", "verifier", "policy"), "abc")}

    def archive(self, *, indexed=3, tagged=True, oci=True, corrupt=False):
        catalog, manifests, files = [], [], {}
        for image in self.images.values():
            catalog.append(dict(Config="blobs/sha256/" + image["id"][7:],
                                RepoTags=[image["tag"]] if tagged else None, Layers=[]))
            data = json.dumps(dict(config=dict(digest=image["id"]))).encode()
            digest = hashlib.sha256(data).hexdigest()
            files["blobs/sha256/" + digest] = data + (b" " if corrupt else b"")
            manifests.append(dict(digest="sha256:" + digest))
        files["manifest.json"] = json.dumps(catalog).encode()
        if oci:
            files["index.json"] = json.dumps(dict(manifests=manifests[:indexed])).encode()
        with tarfile.open(self.path, "w") as archive:
            for name, data in files.items():
                entry = tarfile.TarInfo(name)
                entry.size = len(data)
                archive.addfile(entry, io.BytesIO(data))

    def test_complete_docker_and_oci_catalogs(self):
        self.archive()
        validate_image_archive(self.path, self.images)

    def test_original_anonymous_export_is_rejected(self):
        self.archive(tagged=False, indexed=1)
        with self.assertRaisesRegex(ValueError, "tagged"):
            validate_image_archive(self.path, self.images)

    def test_three_legacy_entries_cannot_hide_incomplete_oci_index(self):
        self.archive(indexed=1)
        with self.assertRaisesRegex(ValueError, "all three"):
            validate_image_archive(self.path, self.images)

    def test_corrupt_oci_manifest_is_rejected(self):
        self.archive(corrupt=True)
        with self.assertRaisesRegex(ValueError, "checksum"):
            validate_image_archive(self.path, self.images)

    def test_older_docker_only_archive_is_supported(self):
        self.archive(oci=False)
        validate_image_archive(self.path, self.images)
