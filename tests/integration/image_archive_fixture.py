"""Small `docker save`-shaped archives: legacy manifest.json plus an OCI index.json."""
import hashlib
import io
import json
import tarfile


def write_image_archive(path, images, *, indexed=None, tagged=True, annotated=True, oci=True,
                        corrupt=False, platform_index=False, names=None):
    """Write an archive for {role: {id, tag}}; keyword arguments reproduce known defects.

    indexed: keep only the first N OCI descriptors (v0.1.0-rc.1 kept one of three).
    tagged / annotated: legacy RepoTags / OCI io.containerd.image.name annotations.
    names: override the annotation per role. Returns {config ID: OCI manifest digest}.
    """
    catalog, descriptors, files, identities = [], [], {}, {}
    for role, image in images.items():
        catalog.append(dict(Config="blobs/sha256/" + image["id"][7:],
                            RepoTags=[image["tag"]] if tagged else None, Layers=[]))
        body = dict(manifests=[]) if platform_index else dict(config=dict(digest=image["id"]))
        data = json.dumps(body).encode()
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        files["blobs/sha256/" + digest[7:]] = data + (b" " if corrupt else b"")
        descriptor = dict(mediaType="application/vnd.oci.image.manifest.v1+json", digest=digest, size=len(data))
        if annotated:
            name = (names or {}).get(role, "docker.io/library/" + image["tag"])
            descriptor["annotations"] = {"io.containerd.image.name": name,
                                         "org.opencontainers.image.ref.name": image["tag"].rsplit(":", 1)[-1]}
        descriptors.append(descriptor)
        identities[image["id"]] = digest
    files["manifest.json"] = json.dumps(catalog).encode()
    if oci:
        files["oci-layout"] = b'{"imageLayoutVersion": "1.0.0"}'
        files["index.json"] = json.dumps(dict(schemaVersion=2, manifests=descriptors[:indexed])).encode()
    with tarfile.open(path, "w") as archive:
        for name, data in files.items():
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            archive.addfile(entry, io.BytesIO(data))
    return identities
