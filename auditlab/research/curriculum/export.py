"""Trusted split planning and train/dev handoff for independent training consumers."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from itertools import combinations
import json
from pathlib import Path
import re
import stat
from tempfile import TemporaryDirectory

from auditlab.evaluator.schema import validate_submission
from auditlab.release.rehearsal import PUBLIC_FILES
from datasets.audit_families import HISTORY_VERSION, fingerprints
from datasets.audit_subhistories import VIEWS, units


SPLITS = ("train", "dev", "test")
SCHEMA_VERSION = "1.0"
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_RESERVED_NAMES = {"aux", "con", "nul", "prn", *(f"com{n}" for n in range(1, 10)),
                   *(f"lpt{n}" for n in range(1, 10))}


def _wire(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _digest(value):
    return sha256(_wire(value)).hexdigest()


def _regular(path, file_type):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    reparse_point = getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    return file_type(info.st_mode) and not reparse_point


def _public_bytes(public):
    public = Path(public)
    if not _regular(public, stat.S_ISDIR):
        raise ValueError("Expected a public snapshot directory")
    files = {}
    for name in PUBLIC_FILES:
        path = public / name
        if not _regular(path, stat.S_ISREG):
            raise ValueError(f"Expected a regular public snapshot file: {name}")
        files[name] = path.read_bytes()
    return files


def _file_hashes(files):
    return {name: sha256(content).hexdigest() for name, content in files.items()}


def snapshot_sha256(public: Path) -> str:
    """Hash the byte identities of the six public snapshot files by name."""
    return _digest(_file_hashes(_public_bytes(public)))


def _source_directory(snapshot):
    private = snapshot / "private"
    source = private / "source_book"
    if (private.exists() or private.is_symlink()) and not _regular(private, stat.S_ISDIR):
        raise ValueError("Expected a regular trusted source book directory")
    return source if source.exists() or source.is_symlink() else None


@dataclass(frozen=True)
class FamilyMember:
    candidate_id: str
    family_id: str
    split: str
    snapshot: Path = field(repr=False)
    parent: str | None = None


@dataclass(frozen=True)
class _BoundMember:
    member: FamilyMember
    public_sha256: str
    public_family: str
    public_book: str
    source_family: str | None
    source_book: str | None
    source_sha256: str | None
    partial: frozenset[tuple[str, str]] = field(repr=False)

    def record(self):
        return {"candidate_id": self.member.candidate_id, "family_id": self.member.family_id,
                "split": self.member.split, "parent": self.member.parent,
                "snapshot_sha256": self.public_sha256, "public_family_sha256": self.public_family,
                "public_book_sha256": self.public_book, "source_family_sha256": self.source_family,
                "source_book_sha256": self.source_book, "source_snapshot_sha256": self.source_sha256}

    def check_source(self):
        source = _source_directory(self.member.snapshot)
        if (None if source is None else snapshot_sha256(source)) != self.source_sha256:
            raise ValueError("Trusted source snapshot changed after planning")


@dataclass(frozen=True)
class FamilyPlan:
    """Trusted frozen split, plus explicitly attached derivatives when applicable."""
    _rows: tuple[_BoundMember, ...] = field(repr=False)
    sha256: str
    _frozen_count: int

    def manifest(self) -> dict:
        members = [row.record() for row in self._rows]
        return {"history_fingerprint_version": HISTORY_VERSION, "partition_sha256": self.sha256,
                "membership_sha256": _digest(members), "frozen_count": self._frozen_count,
                "members": members, "partial_overlap": _partial_overlap(self._rows)}

    def assert_searchable(self, candidate_id: str) -> FamilyMember:
        for row in self._rows:
            if row.member.candidate_id == candidate_id and row.member.split == "train":
                if snapshot_sha256(row.member.snapshot / "public") != row.public_sha256:
                    raise ValueError("Public snapshot changed after planning")
                row.check_source()
                return row.member
        raise ValueError("Search requires a candidate in the frozen train families")

    def assert_search_family(self, family_id: str) -> None:
        if not any(row.member.family_id == family_id and row.member.split == "train" for row in self._rows):
            raise ValueError("Search requires a frozen train family")


def _bind(member):
    candidate_id = member.candidate_id
    if (not isinstance(candidate_id, str) or not _SAFE_ID.fullmatch(candidate_id)
            or candidate_id.endswith(".") or candidate_id.split(".")[0].lower() in _RESERVED_NAMES):
        raise ValueError("Candidate ID must be a portable path component")
    if not isinstance(member.family_id, str) or not member.family_id.strip() or member.split not in SPLITS:
        raise ValueError("Every candidate requires an economic family and a train/dev/test split")

    snapshot = Path(member.snapshot).absolute()
    public = snapshot / "public"
    public_hash = snapshot_sha256(public)
    final = fingerprints(public)
    original = _source_directory(snapshot)
    source_hash = None if original is None else snapshot_sha256(original)
    source = {} if original is None else fingerprints(original)
    partial = frozenset((view, _digest(value)) for view, _, _, value in units(public))
    return _BoundMember(FamilyMember(candidate_id, member.family_id, member.split, snapshot, member.parent),
                        public_hash, final["family"], final["book"], source.get("family"), source.get("book"),
                        source_hash, partial)


def _partial_overlap(rows):
    by_split = {split: {view: set() for view in VIEWS} for split in SPLITS}
    for row in rows:
        for view, digest in row.partial:
            by_split[row.member.split][view].add(digest)
    return {"shared_distinct_complete_chains": {
        f"{left}/{right}": {view: len(by_split[left][view] & by_split[right][view]) for view in VIEWS}
        for left, right in combinations(SPLITS, 2)
    }}


def _check_partition(rows):
    by_id = {row.member.candidate_id: row.member for row in rows}
    if len({key.lower() for key in by_id}) != len(rows):
        raise ValueError("Duplicate candidate ID")

    split_by_origin = {}
    for row in rows:
        member = row.member
        links = (("declared family", member.family_id), ("economic family", row.public_family),
                 ("economic family", row.source_family), ("posted book", row.public_book),
                 ("posted book", row.source_book))
        for kind, identity in links:
            if identity is not None and split_by_origin.setdefault((kind, identity), member.split) != member.split:
                raise ValueError(f"Cross-split {kind} overlap")

    for member in by_id.values():
        seen = {member.candidate_id}
        parent_id = member.parent
        while parent_id is not None:
            parent = by_id.get(parent_id)
            if parent is None or parent.family_id != member.family_id or parent.split != member.split:
                raise ValueError("A derivative requires a declared parent in the same family and split")
            if parent_id in seen:
                raise ValueError("Family lineage contains a cycle")
            seen.add(parent_id)
            parent_id = parent.parent


def plan_splits(members: Iterable[FamilyMember]) -> FamilyPlan:
    """Freeze the roster before running agents and check whole economic histories across splits."""
    rows = tuple(sorted(map(_bind, members), key=lambda row: row.member.candidate_id))
    if not rows:
        raise ValueError("A split plan requires at least one candidate")
    _check_partition(rows)
    return FamilyPlan(rows, _digest([row.record() for row in rows]), len(rows))


def extend_family_plan(plan: FamilyPlan, members: Iterable[FamilyMember]) -> FamilyPlan:
    """Bind train/dev derivatives to the pre-existing roster without changing holdout."""
    additions = tuple(members)
    if any(member.split == "test" or member.parent is None for member in additions):
        raise ValueError("Only train/dev derivatives can be added to a frozen partition")
    rows = tuple(sorted((*plan._rows, *map(_bind, additions)), key=lambda row: row.member.candidate_id))
    _check_partition(rows)
    return FamilyPlan(rows, plan.sha256, plan._frozen_count)


@dataclass(frozen=True)
class CurriculumExample:
    candidate_id: str
    provenance: Mapping = field(repr=False)
    validation: Mapping = field(repr=False)
    selection_cost: Mapping
    teacher_output: Mapping | None = field(default=None, repr=False)


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_wire(value) + b"\n")
    return sha256(path.read_bytes()).hexdigest()


def _check_cost(value):
    if isinstance(value, (int, float)) and value < 0:
        raise ValueError("Selection cost must be nonnegative")
    if isinstance(value, Mapping):
        for item in value.values():
            _check_cost(item)
    if isinstance(value, (list, tuple)):
        for item in value:
            _check_cost(item)


def export_curriculum(plan: FamilyPlan, examples: Iterable[CurriculumExample], output_dir: Path, *,
                      artifact_id: str, source_sha: str, protocol_id: str) -> Path:
    """Export verified train/dev snapshots and trusted metadata to a fresh directory.

    A policy run receives the tool view of one ``public/<split>/<snapshot_sha256>`` leaf.
    The assembly and teacher files belong to the trusted trainer. Full dev receipts
    and configs stay with the evaluator. Test members are omitted from the handoff.
    """
    if not all(isinstance(value, str) and value.strip() for value in (artifact_id, source_sha, protocol_id)):
        raise ValueError("Curriculum artifact, source and protocol IDs are required")
    output = Path(output_dir).absolute()
    if output.exists() or output.is_symlink():
        raise FileExistsError(output)
    by_id = {row.member.candidate_id: row for row in plan._rows}
    examples = tuple(examples)
    if len({example.candidate_id for example in examples}) != len(examples):
        raise ValueError("Duplicate curriculum example")
    candidates = []
    for example in examples:
        item = by_id.get(example.candidate_id)
        if item is None or item.member.split == "test":
            raise ValueError("Curriculum export requires a planned train/dev candidate")
        candidates.append((item, example))
    # Prefer a labeled representative, then the base case, before the stable ID tie-break.
    candidates.sort(key=lambda pair: (pair[1].teacher_output is None, pair[0].member.parent is not None,
                                      pair[0].member.candidate_id))

    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".curriculum-", dir=output.parent) as directory:
        root = Path(directory) / "bundle"
        root.mkdir()
        seen_books = {}
        results = [_export_example(root, item, example, seen_books) for item, example in candidates]
        manifest = {
            "schema_version": SCHEMA_VERSION, "artifact_id": artifact_id, "source_sha": source_sha,
            "protocol_id": protocol_id, "partition_sha256": plan.sha256,
            "history_fingerprint_version": HISTORY_VERSION,
            "formats": {"snapshots": "auditlab-public-snapshot-v1", "teacher": "auditlab-teacher-json-v1"},
            "planned_counts": {split: sum(row.member.split == split for row in plan._rows) for split in SPLITS},
            "partial_overlap": _partial_overlap(plan._rows),
            "examples": sorted(results, key=lambda row: row["candidate_id"]),
        }
        _write_json(root / "curriculum.json", manifest)
        root.rename(output)
    return output / "curriculum.json"


def _export_example(root, item, example, seen_books):
    member = item.member
    if example.teacher_output is not None and (member.split != "train"
                                               or not isinstance(example.teacher_output, Mapping)):
        raise ValueError("Teacher output requires a trusted training example")
    if not all(isinstance(value, Mapping) for value in (example.provenance, example.validation,
                                                        example.selection_cost)):
        raise ValueError("Curriculum examples require structured provenance, validation and selection cost")
    files = _public_bytes(member.snapshot / "public")
    hashes = _file_hashes(files)
    if _digest(hashes) != item.public_sha256:
        raise ValueError("Public snapshot changed after planning")
    item.check_source()
    if (example.validation.get("status") != "passed"
            or example.validation.get("snapshot_sha256") != item.public_sha256):
        raise ValueError("Independent acceptance receipt must pass for these exact public bytes")
    teacher_output = None
    if example.teacher_output is not None:
        teacher_output = dict(example.teacher_output)
        submission = teacher_output.get("submission", teacher_output)
        validate_submission(submission, json.loads(files["manifest.json"])["episode_id"])
    _check_cost(example.selection_cost)
    provenance = dict(example.provenance)
    validation = dict(example.validation)
    result = {**item.record(), "provenance_sha256": _digest(provenance),
              "validation_sha256": _digest(validation), "selection_cost": dict(example.selection_cost),
              "files_sha256": hashes}
    if member.split == "train":
        result.update(provenance=provenance, validation=validation)
    else:
        # A full dev receipt may contain independently calculated answers. Even its
        # generator config may identify the fault. Keep the details with the evaluator.
        result.update(provenance=None, validation={"status": "passed", "snapshot_sha256": item.public_sha256})

    # Reordered aliases share the posted book and operative policy. Keep a deterministic
    # representative while retaining both costs and all validation receipts in the handoff.
    key = (member.split, item.public_book, _digest(json.loads(files["policy.json"])))
    duplicate_of = seen_books.setdefault(key, member.candidate_id)
    if duplicate_of != member.candidate_id:
        return {**result, "status": "filtered", "reason": "equivalent_posted_book",
                "duplicate_of": duplicate_of, "snapshot": None, "teacher": None}

    path = Path("public") / member.split / item.public_sha256
    (root / path).mkdir(parents=True)
    for name, content in files.items():
        (root / path / name).write_bytes(content)
    teacher = None
    if teacher_output is not None:
        reference = Path("trusted/train") / member.candidate_id / "teacher.json"
        teacher = {"path": reference.as_posix(), "sha256": _write_json(root / reference, teacher_output)}
    return {**result, "status": "included", "reason": None, "snapshot": path.as_posix(), "teacher": teacher}
