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

from .contracts import ContractError, EvidenceRef, new_id, validate_id


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
        for manifest in self.root.glob("run_*/index.jsonl"):
            resolve_approved_path(str(manifest), [str(self.root)])
            with manifest.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        payload = json.loads(line)
                        reference = EvidenceRef.from_json(payload)
                        resolve_approved_path(reference.path, [str(self.root)], must_exist=True)
                    except (ValueError, OSError):
                        continue  # incomplete append or previously pruned evidence
                    self._index[reference.evidence_id] = reference
                    if payload.get("retained"):
                        self._keep.add(reference.evidence_id)

    # -- writing -------------------------------------------------------------------

    def run_dir(self, run_id: str) -> Path:
        validate_id("run", run_id)
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
        name = f"{new_id('ev').split(':')[1]}{suffix}"
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
        self.register(reference)
        if keep:
            self.retain(reference.evidence_id)
        return reference

    def register(self, reference: EvidenceRef) -> EvidenceRef:
        resolve_approved_path(reference.path, [str(self.root)], must_exist=True)
        if reference.evidence_id not in self._index:
            self._append_reference(reference)
        self._index[reference.evidence_id] = reference
        return reference

    def retain(self, evidence_id: str) -> None:
        self._keep.add(evidence_id)
        self._append_reference(self.get(evidence_id))

    def _append_reference(self, reference: EvidenceRef) -> None:
        payload = {**reference.to_json(), "retained": reference.evidence_id in self._keep}
        with (self.run_dir(reference.run_id) / "index.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def get(self, evidence_id: str) -> EvidenceRef:
        reference = self._index.get(evidence_id)
        if reference is None:
            raise ContractError(f"unknown evidence reference {evidence_id}")
        return reference

    def read(self, evidence_id: str) -> bytes:
        reference = self.get(evidence_id)
        path = resolve_approved_path(reference.path, [str(self.root)], must_exist=True)
        with open(path, "rb") as handle:
            data = handle.read()
        if len(data) != reference.size_bytes or hashlib.sha256(data).hexdigest() != reference.sha256:
            raise ContractError("evidence bytes no longer match the recorded digest")
        return data

    # -- retention -----------------------------------------------------------------

    def prune(self, *, now: float | None = None) -> dict[str, int]:
        """Delete aged evidence, but keep failure/visual-boundary evidence until the hard cap."""
        moment = time.time() if now is None else now
        removed, kept, freed = 0, 0, 0
        changed_runs: set[str] = set()
        for reference in list(self._index.values()):
            age = moment - reference.created_at
            expired = age > self.retention.max_age_seconds
            if not expired or reference.evidence_id in self._keep:
                kept += 1
                continue
            try:
                os.remove(reference.path)
            except FileNotFoundError:
                pass
            except OSError:
                kept += 1
                continue
            freed += reference.size_bytes
            removed += 1
            self._index.pop(reference.evidence_id, None)
            self._keep.discard(reference.evidence_id)
            changed_runs.add(reference.run_id)
        total = sum(reference.size_bytes for reference in self._index.values())
        run_bytes: dict[str, int] = {}
        for reference in self._index.values():
            run_bytes[reference.run_id] = run_bytes.get(reference.run_id, 0) + reference.size_bytes
        if total > self.retention.max_total_bytes or any(
            value > self.retention.max_run_bytes for value in run_bytes.values()
        ):
            ordered = sorted(self._index.values(), key=lambda item: (item.evidence_id in self._keep, item.created_at))
            for reference in ordered:
                if (
                    total <= self.retention.max_total_bytes
                    and run_bytes[reference.run_id] <= self.retention.max_run_bytes
                ):
                    continue
                try:
                    os.remove(reference.path)
                except FileNotFoundError:
                    pass
                except OSError:
                    continue
                total -= reference.size_bytes
                run_bytes[reference.run_id] -= reference.size_bytes
                freed += reference.size_bytes
                removed += 1
                self._index.pop(reference.evidence_id, None)
                self._keep.discard(reference.evidence_id)
                changed_runs.add(reference.run_id)
        self._compact_manifests(changed_runs)
        return {"removed": removed, "kept": kept, "freed_bytes": freed}

    def _compact_manifests(self, run_ids: set[str]) -> None:
        remaining: dict[str, list[EvidenceRef]] = {run_id: [] for run_id in run_ids}
        for reference in self._index.values():
            if reference.run_id in remaining:
                remaining[reference.run_id].append(reference)
        for run_id, references in remaining.items():
            directory = self.root / run_id.replace(":", "_")
            manifest = directory / "index.jsonl"
            if not references:
                manifest.unlink(missing_ok=True)
                with contextlib.suppress(OSError):
                    directory.rmdir()
                continue
            temporary = directory / "index.jsonl.tmp"
            with temporary.open("w", encoding="utf-8") as handle:
                for reference in references:
                    payload = {**reference.to_json(), "retained": reference.evidence_id in self._keep}
                    handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
            temporary.replace(manifest)

    def usage(self) -> dict[str, int]:
        return {
            "references": len(self._index),
            "bytes": sum(reference.size_bytes for reference in self._index.values()),
            "disc_root_bytes": sum(child.stat().st_size for child in self.root.rglob("*") if child.is_file()),
        }
