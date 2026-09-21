"""Evidence storage: scoped capture references, approved artifact paths, retention.

Binary evidence lives outside the journal; the journal stores references. Artifact
assertions may only touch approved roots, and path escapes and reparse-point traversal are
rejected rather than normalized away.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from .contracts import ContractError, EvidenceRef, new_id


def _real(path: str | os.PathLike[str]) -> str:
    return os.path.realpath(os.fspath(path))


def _has_reparse_component(path: str) -> bool:
    """Reject symlink/junction traversal: every component must be a plain directory."""
    current = Path(path)
    while True:
        try:
            info = os.lstat(current)
        except OSError:
            break
        reparse_tag = getattr(info, "st_reparse_tag", None)
        if os.path.islink(current) or reparse_tag:
            return True
        if current.parent == current:
            break
        current = current.parent
    return False


def resolve_approved_path(path: str, approved_roots: list[str], *, must_exist: bool = False) -> str:
    """Resolve a path that must stay inside an approved root, with no reparse traversal."""
    if not approved_roots:
        raise ContractError("no approved artifact roots are configured")
    candidate = _real(path)
    approved = False
    for root in approved_roots:
        real_root = _real(root)
        if candidate == real_root or candidate.startswith(real_root.rstrip(os.sep) + os.sep):
            approved = True
            break
    if not approved:
        raise ContractError(f"path is outside the approved artifact roots: {path}")
    if _has_reparse_component(candidate):
        raise ContractError(f"path crosses a link or reparse point: {path}")
    if must_exist and not os.path.isfile(candidate):
        raise ContractError(f"artifact does not exist: {candidate}")
    return candidate


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass
class RetentionPolicy:
    max_age_seconds: float = 7 * 24 * 3600
    max_total_bytes: int = 2 * 1024 * 1024 * 1024
    max_run_bytes: int = 256 * 1024 * 1024


@dataclass
class EvidenceStore:
    root: Path
    approved_roots: list[str]
    retention: RetentionPolicy = field(default_factory=RetentionPolicy)
    _index: dict[str, EvidenceRef] = field(default_factory=dict)
    _keep: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self.root.mkdir(parents=True, exist_ok=True)

    # -- writing -------------------------------------------------------------------

    def run_dir(self, run_id: str) -> Path:
        directory = self.root / run_id.replace(":", "_")
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def save_bytes(
        self,
        *,
        run_id: str,
        checkpoint: str | None,
        description: str,
        data: bytes,
        kind: str,
        media_type: str,
        suffix: str = ".bin",
        keep: bool = False,
    ) -> EvidenceRef:
        directory = self.run_dir(run_id)
        name = f"{(checkpoint or kind)[:40].replace('/', '-')}-{int(time.time() * 1000)}{suffix}"
        path = directory / name
        path.write_bytes(data)
        reference = EvidenceRef(
            evidence_id=new_id("ev"),
            run_id=run_id,
            kind=kind,
            path=str(path),
            media_type=media_type,
            sha256=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
            created_at=time.time(),
            checkpoint=checkpoint,
            description=description,
        )
        self._index[reference.evidence_id] = reference
        (directory / "index.jsonl").open("a", encoding="utf-8").write(
            json.dumps(reference.to_json(), ensure_ascii=False) + "\n"
        )
        if keep:
            self._keep.add(reference.evidence_id)
        return reference

    def register(self, reference: EvidenceRef) -> EvidenceRef:
        self._index[reference.evidence_id] = reference
        return reference

    def get(self, evidence_id: str) -> EvidenceRef:
        reference = self._index.get(evidence_id)
        if reference is None:
            raise ContractError(f"unknown evidence reference {evidence_id}")
        return reference

    def read(self, evidence_id: str) -> bytes:
        reference = self.get(evidence_id)
        with open(reference.path, "rb") as handle:
            return handle.read()

    # -- retention -----------------------------------------------------------------

    def prune(self, *, now: float | None = None) -> dict[str, int]:
        """Delete aged evidence, but keep failure/visual-boundary evidence until the hard cap."""
        moment = now or time.time()
        removed, kept, freed = 0, 0, 0
        for reference in list(self._index.values()):
            age = moment - reference.created_at
            expired = age > self.retention.max_age_seconds
            if not expired or reference.evidence_id in self._keep:
                kept += 1
                continue
            with contextlib.suppress(OSError):
                os.remove(reference.path)
            freed += reference.size_bytes
            removed += 1
            self._index.pop(reference.evidence_id, None)
        total = sum(reference.size_bytes for reference in self._index.values())
        if total > self.retention.max_total_bytes:  # hard cap: oldest first, keep-set last
            ordered = sorted(self._index.values(), key=lambda item: (item.evidence_id in self._keep, item.created_at))
            for reference in ordered:
                if total <= self.retention.max_total_bytes:
                    break
                try:
                    os.remove(reference.path)
                except OSError:
                    continue
                total -= reference.size_bytes
                freed += reference.size_bytes
                removed += 1
                self._index.pop(reference.evidence_id, None)
        return {"removed": removed, "kept": kept, "freed_bytes": freed}

    def usage(self) -> dict[str, int]:
        return {
            "references": len(self._index),
            "bytes": sum(reference.size_bytes for reference in self._index.values()),
            "disc_root_bytes": sum(child.stat().st_size for child in self.root.rglob("*") if child.is_file()),
        }
