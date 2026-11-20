"""Every saved image must be listed, with its tag, for both Docker image stores.

The classic store reads manifest.json; the containerd store reads only index.json and
names images from io.containerd.image.name. v0.1.0-rc.1 passed classic and lost two
of three images on containerd.
"""
from pathlib import Path
import tempfile
import unittest

from image_archive_fixture import write_image_archive
import offline
from tools.build_release import require_classic_build_store, validate_image_archive


class ImageArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "images.tar"
        self.images = {role: dict(id="sha256:" + digit * 64, tag="auditlab-" + role + ":test")
                       for role, digit in zip(("runtime", "verifier", "policy"), "abc")}

    def archive(self, **defects):
        return write_image_archive(self.path, self.images, **defects)

    def test_builder_and_launcher_share_one_contract(self):
        self.assertIs(validate_image_archive, offline.validate_image_archive)

    def test_release_builder_refuses_the_containerd_store_with_guidance(self):
        require_classic_build_store('[["Backing Filesystem","extfs"],["Supports d_type","true"]]')
        with self.assertRaisesRegex(ValueError, "classic Docker image store"):
            require_classic_build_store('[["driver-type","io.containerd.snapshotter.v1"]]')

    def test_every_image_is_referenced_from_the_oci_index_with_its_tag(self):
        identities = self.archive()
        self.assertEqual(validate_image_archive(self.path, self.images), identities)
        recorded = {role: dict(image, oci_manifest_digest=identities[image["id"]]) for role, image in self.images.items()}
        self.assertEqual(validate_image_archive(self.path, recorded), identities)

    def test_original_anonymous_export_is_rejected(self):
        self.archive(tagged=False, annotated=False, indexed=1)
        with self.assertRaisesRegex(ValueError, "tagged"):
            validate_image_archive(self.path, self.images)

    def test_three_legacy_entries_cannot_hide_incomplete_oci_index(self):
        self.archive(indexed=1)
        with self.assertRaisesRegex(ValueError, "all three"):
            validate_image_archive(self.path, self.images)

    def test_untagged_oci_entries_are_rejected(self):
        self.archive(annotated=False)
        with self.assertRaisesRegex(ValueError, "not tagged"):
            validate_image_archive(self.path, self.images)

    def test_oci_tag_must_belong_to_the_same_image(self):
        swapped = {"runtime": "docker.io/library/auditlab-policy:test", "policy": "auditlab-runtime:test"}
        self.archive(names=swapped)
        with self.assertRaisesRegex(ValueError, "with its tag"):
            validate_image_archive(self.path, self.images)

    def test_foreign_oci_tag_is_rejected(self):
        self.archive(names={"verifier": "docker.io/library/unrelated:latest"})
        with self.assertRaisesRegex(ValueError, "not tagged"):
            validate_image_archive(self.path, self.images)

    def test_recorded_oci_manifest_must_match_the_archive(self):
        self.archive()
        changed = dict(self.images, runtime=dict(self.images["runtime"], oci_manifest_digest="sha256:" + "f" * 64))
        with self.assertRaisesRegex(ValueError, "differs from the bundle manifest"):
            validate_image_archive(self.path, changed)

    def test_multi_platform_index_entries_are_rejected(self):
        self.archive(platform_index=True)
        with self.assertRaisesRegex(ValueError, "single-platform"):
            validate_image_archive(self.path, self.images)

    def test_corrupt_oci_manifest_is_rejected(self):
        self.archive(corrupt=True)
        with self.assertRaisesRegex(ValueError, "checksum"):
            validate_image_archive(self.path, self.images)

    def test_older_docker_only_archive_is_supported(self):
        self.archive(oci=False)
        self.assertEqual(validate_image_archive(self.path, self.images), {})

    def test_docker_only_archive_cannot_drop_a_recorded_oci_index(self):
        identities = self.archive(oci=False)
        recorded = {role: dict(image, oci_manifest_digest=identities[image["id"]]) for role, image in self.images.items()}
        with self.assertRaisesRegex(ValueError, "lost the OCI index"):
            validate_image_archive(self.path, recorded)


if __name__ == "__main__":
    unittest.main()
