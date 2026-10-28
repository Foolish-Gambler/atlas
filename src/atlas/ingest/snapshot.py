"""Immutable, versioned financial dataset snapshots. Local disk only."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from atlas.contracts import (
    EnsemblePrediction,
    FilingRecord,
    MarketBarRecord,
    NewsRecord,
    Provenance,
    ResearchArtifact,
    ReturnPrediction,
    SourceKind,
    StatementRecord,
    StreamEvent,
    TimeIndex,
    XbrlFactRecord,
)
from atlas.ingest.codec import (
    bar_to_row,
    build_stream,
    dumps_canonical,
    ensemble_to_row,
    fact_to_row,
    filing_to_row,
    news_to_row,
    parse_bar,
    parse_ensemble,
    parse_fact,
    parse_filing,
    parse_news,
    parse_prediction,
    parse_research,
    parse_statement,
    prediction_to_row,
    research_to_row,
    statement_to_row,
)
from atlas.ingest.local import LocalFixtureSource
from atlas.validation import InvalidInputError, normalize_symbol

SCHEMA_VERSION = "1"
MANIFEST_NAME = "manifest.json"
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class SnapshotError(ValueError):
    """Base error for snapshot storage."""


class RevisionExistsError(SnapshotError):
    """A dataset revision is immutable and already on disk."""


class SnapshotIntegrityError(SnapshotError):
    """Content hashes or directory contents do not match the manifest."""


class UnsupportedSchemaError(SnapshotError):
    """manifest schema_version is not supported by this ATLAS build."""


@dataclass(frozen=True, slots=True)
class SnapshotPayload:
    bars: tuple[MarketBarRecord, ...] = ()
    filings: tuple[FilingRecord, ...] = ()
    news: tuple[NewsRecord, ...] = ()
    statements: tuple[StatementRecord, ...] = ()
    research: tuple[ResearchArtifact, ...] = ()
    predictions: tuple[ReturnPrediction, ...] = ()
    ensembles: tuple[EnsemblePrediction, ...] = ()
    facts: tuple[XbrlFactRecord, ...] = ()
    vendor_raw: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class SnapshotManifest:
    schema_version: str
    dataset_id: str
    revision: str
    source_kind: SourceKind
    created_at: datetime
    ingested_at: datetime
    files: dict[str, str]
    symbols: tuple[str, ...]
    counts: dict[str, int]
    vendor: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "schema_version": self.schema_version,
            "dataset_id": self.dataset_id,
            "revision": self.revision,
            "source_kind": self.source_kind.value,
            "vendor": self.vendor,
            "created_at": self.created_at.isoformat(),
            "ingested_at": self.ingested_at.isoformat(),
            "files": dict(sorted(self.files.items())),
            "symbols": list(self.symbols),
            "counts": dict(sorted(self.counts.items())),
        }
        if self.metadata:
            payload["metadata"] = self.metadata
        return payload

    @property
    def execution_eligible(self) -> bool:
        if "execution_eligible" in self.metadata:
            return bool(self.metadata["execution_eligible"])
        return self.source_kind is not SourceKind.VENDOR_READ_ONLY


class SnapshotSource:
    """Read-only loader for a verified snapshot revision."""

    def __init__(self, payload: SnapshotPayload, manifest: SnapshotManifest) -> None:
        self.manifest = manifest
        self.provenance = Provenance(
            source_kind=manifest.source_kind,
            dataset_id=manifest.dataset_id,
            revision=manifest.revision,
            vendor=manifest.vendor,
        )
        self._bars = payload.bars
        self._filings = payload.filings
        self._news = payload.news
        self._statements = payload.statements
        self._research = payload.research
        self._predictions = payload.predictions
        self._ensembles = payload.ensembles
        self._facts = payload.facts
        self._vendor_raw = payload.vendor_raw
        self._by_ref = {bar.ref_id: bar for bar in self._bars}
        self._events = build_stream(
            self._bars,
            self._filings,
            self._news,
            self._statements,
            self._research,
            self._predictions,
            self._ensembles,
            self._facts,
        )

    def bars(self, symbol: str) -> Sequence[MarketBarRecord]:
        key = normalize_symbol(symbol)
        return tuple(bar for bar in self._bars if bar.symbol == key)

    def all_bars(self) -> tuple[MarketBarRecord, ...]:
        return self._bars

    def filings(self) -> tuple[FilingRecord, ...]:
        return self._filings

    def news(self) -> tuple[NewsRecord, ...]:
        return self._news

    def statements(self) -> tuple[StatementRecord, ...]:
        return self._statements

    def research(self) -> tuple[ResearchArtifact, ...]:
        return self._research

    def predictions(self) -> tuple[ReturnPrediction, ...]:
        return self._predictions

    def ensembles(self) -> tuple[EnsemblePrediction, ...]:
        return self._ensembles

    def facts(self) -> tuple[XbrlFactRecord, ...]:
        return self._facts

    def vendor_raw(self) -> tuple[dict[str, Any], ...]:
        return self._vendor_raw

    def stream(self) -> Sequence[StreamEvent]:
        return self._events

    def bar_by_ref(self, ref_id: str) -> MarketBarRecord:
        try:
            return self._by_ref[ref_id]
        except KeyError as exc:
            raise InvalidInputError(f"unknown bar ref_id {ref_id!r}") from exc


class SnapshotStore:
    """Append-only dataset revisions under `{root}/{dataset_id}/{revision}/`."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def revision_path(self, dataset_id: str, revision: str) -> Path:
        dataset_id = _require_id(dataset_id, "dataset_id")
        revision = _require_id(revision, "revision")
        return self.root / dataset_id / revision

    def create(
        self,
        dataset_id: str,
        revision: str,
        payload: SnapshotPayload,
        *,
        source_kind: SourceKind,
        vendor: str | None = None,
        created_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> SnapshotManifest:
        dataset_id = _require_id(dataset_id, "dataset_id")
        revision = _require_id(revision, "revision")
        if source_kind is SourceKind.VENDOR_READ_ONLY and not vendor:
            raise InvalidInputError("vendor_read_only snapshots must name a vendor.")
        stamp = created_at or datetime.now(UTC)
        if stamp.tzinfo is None:
            raise InvalidInputError("created_at must be timezone-aware.")
        payload = _stamp_payload(payload, stamp)
        dest = self.revision_path(dataset_id, revision)
        if dest.exists():
            raise RevisionExistsError(
                f"dataset {dataset_id!r} revision {revision!r} already exists and is immutable."
            )
        parent = dest.parent
        parent.mkdir(parents=True, exist_ok=True)
        tmp = parent / f".{revision}.tmp-{uuid4().hex}"
        published = False
        try:
            files, counts, symbols = _write_payload(tmp, payload)
            manifest = SnapshotManifest(
                schema_version=SCHEMA_VERSION,
                dataset_id=dataset_id,
                revision=revision,
                source_kind=source_kind,
                created_at=stamp,
                ingested_at=stamp,
                files=files,
                symbols=symbols,
                counts=counts,
                vendor=vendor,
                metadata=dict(metadata or {}),
            )
            (tmp / MANIFEST_NAME).write_bytes(dumps_canonical(manifest.to_dict()))
            tmp.replace(dest)
            published = True
        except Exception:
            if tmp.exists():
                shutil.rmtree(tmp, ignore_errors=True)
            if not published and dest.exists() and not (dest / MANIFEST_NAME).is_file():
                shutil.rmtree(dest, ignore_errors=True)
            raise
        if dest.exists() and tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)
        return manifest

    def create_from_fixture(
        self,
        fixture_path: Path,
        *,
        dataset_id: str | None = None,
        revision: str | None = None,
        created_at: datetime | None = None,
    ) -> SnapshotManifest:
        source = LocalFixtureSource(Path(fixture_path))
        payload = SnapshotPayload(
            bars=source.all_bars(),
            filings=source.filings(),
            news=source.news(),
            statements=source.statements(),
            research=source.research(),
            predictions=source.predictions(),
            ensembles=source.ensembles(),
            facts=source.facts(),
        )
        return self.create(
            dataset_id or source.provenance.dataset_id,
            revision or source.provenance.revision,
            payload,
            source_kind=source.provenance.source_kind,
            vendor=source.provenance.vendor,
            created_at=created_at,
        )

    def inspect(self, dataset_id: str, revision: str) -> SnapshotManifest:
        return _read_manifest(self.revision_path(dataset_id, revision))

    def verify(self, dataset_id: str, revision: str) -> SnapshotManifest:
        path = self.revision_path(dataset_id, revision)
        manifest = _read_manifest(path)
        _verify_files(path, manifest)
        return manifest

    def load(self, dataset_id: str, revision: str) -> SnapshotSource:
        path = self.revision_path(dataset_id, revision)
        manifest = _read_manifest(path)
        _verify_files(path, manifest)
        payload = _read_payload(path, manifest)
        return SnapshotSource(payload, manifest)


def _require_id(value: str, name: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise InvalidInputError(
            f"{name} must be a path-safe identifier (letters, digits, '.', '_', '-')."
        )
    if value.startswith("."):
        raise InvalidInputError(f"{name} cannot start with '.'")
    return value


def _safe_file_stem(value: str, name: str) -> str:
    cleaned = value.strip().replace("/", "-").replace("\\", "-")
    if not _FILE_RE.fullmatch(cleaned):
        raise InvalidInputError(f"{name} is not a safe snapshot filename.")
    return cleaned


def _stamp_time(time: TimeIndex, ingested: datetime) -> TimeIndex:
    if time.ingested_time is not None:
        return time
    return replace(time, ingested_time=ingested)


def _stamp_payload(payload: SnapshotPayload, ingested: datetime) -> SnapshotPayload:
    bars = tuple(
        bar if bar.ingested_time is not None else replace(bar, ingested_time=ingested)
        for bar in payload.bars
    )
    filings = tuple(
        replace(item, time=_stamp_time(item.time, ingested)) for item in payload.filings
    )
    news = tuple(replace(item, time=_stamp_time(item.time, ingested)) for item in payload.news)
    statements = tuple(
        replace(item, time=_stamp_time(item.time, ingested)) for item in payload.statements
    )
    research = tuple(
        replace(item, time=_stamp_time(item.time, ingested)) for item in payload.research
    )
    predictions = tuple(
        replace(item, time=_stamp_time(item.time, ingested)) for item in payload.predictions
    )
    ensembles = tuple(
        replace(item, time=_stamp_time(item.time, ingested)) for item in payload.ensembles
    )
    facts = tuple(replace(item, time=_stamp_time(item.time, ingested)) for item in payload.facts)
    return SnapshotPayload(
        bars=bars,
        filings=filings,
        news=news,
        statements=statements,
        research=research,
        predictions=predictions,
        ensembles=ensembles,
        facts=facts,
        vendor_raw=payload.vendor_raw,
    )


def _write_payload(
    root: Path, payload: SnapshotPayload
) -> tuple[dict[str, str], dict[str, int], tuple[str, ...]]:
    root.mkdir(parents=True, exist_ok=False)
    files: dict[str, str] = {}

    by_symbol: dict[str, list[MarketBarRecord]] = {}
    for bar in payload.bars:
        by_symbol.setdefault(bar.symbol, []).append(bar)
    if by_symbol:
        (root / "bars").mkdir()
        for symbol, rows in sorted(by_symbol.items()):
            rel = f"bars/{symbol}.jsonl"
            files[rel] = _write_jsonl(root / rel, [bar_to_row(bar) for bar in rows])

    def write_json_dir(dirname: str, items: Sequence[Any], name_fn, to_row) -> None:
        if not items:
            return
        (root / dirname).mkdir()
        used: set[str] = set()
        for item in items:
            stem = _safe_file_stem(name_fn(item), dirname)
            if stem in used:
                raise InvalidInputError(f"duplicate {dirname} filename {stem!r}")
            used.add(stem)
            rel = f"{dirname}/{stem}.json"
            files[rel] = _write_bytes(root / rel, dumps_canonical(to_row(item)))

    write_json_dir("filings", payload.filings, lambda item: item.accession, filing_to_row)
    write_json_dir(
        "news",
        payload.news,
        lambda item: f"{item.symbol}-{item.time.event_time.strftime('%Y%m%dT%H%M%S')}",
        news_to_row,
    )
    write_json_dir(
        "statements",
        payload.statements,
        lambda item: (
            f"{item.symbol}-{item.time.event_time.strftime('%Y%m%d')}-{item.accession}"
            if item.accession
            else f"{item.symbol}-{item.time.event_time.strftime('%Y%m%d')}"
        ),
        statement_to_row,
    )
    write_json_dir("research", payload.research, lambda item: item.artifact_id, research_to_row)
    write_json_dir(
        "predictions", payload.predictions, lambda item: item.prediction_id, prediction_to_row
    )
    write_json_dir("ensembles", payload.ensembles, lambda item: item.ensemble_id, ensemble_to_row)
    by_fact_symbol: dict[str, list[XbrlFactRecord]] = {}
    for fact in payload.facts:
        by_fact_symbol.setdefault(fact.symbol, []).append(fact)
    if by_fact_symbol:
        (root / "facts").mkdir()
        for symbol, rows in sorted(by_fact_symbol.items()):
            rel = f"facts/{symbol}.jsonl"
            files[rel] = _write_jsonl(root / rel, [fact_to_row(item) for item in rows])
    if payload.vendor_raw:
        (root / "vendor").mkdir()
        rel = "vendor/raw_bars.jsonl"
        files[rel] = _write_jsonl(root / rel, [dict(row) for row in payload.vendor_raw])

    counts = {
        "bars": len(payload.bars),
        "filings": len(payload.filings),
        "news": len(payload.news),
        "statements": len(payload.statements),
        "research": len(payload.research),
        "predictions": len(payload.predictions),
        "ensembles": len(payload.ensembles),
        "facts": len(payload.facts),
        "vendor_raw": len(payload.vendor_raw),
    }
    symbols = tuple(sorted({bar.symbol for bar in payload.bars}))
    return files, counts, symbols


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> str:
    blob = b"".join(dumps_canonical(row) for row in rows)
    return _write_bytes(path, blob)


def _write_bytes(path: Path, blob: bytes) -> str:
    path.write_bytes(blob)
    return hashlib.sha256(blob).hexdigest()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_manifest(path: Path) -> SnapshotManifest:
    manifest_path = path / MANIFEST_NAME
    if not path.is_dir() or not manifest_path.is_file():
        raise SnapshotError(f"snapshot revision not found: {path}")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SnapshotError("manifest.json is not valid JSON.") from exc
    version = str(payload.get("schema_version", ""))
    if version != SCHEMA_VERSION:
        raise UnsupportedSchemaError(
            f"unsupported schema_version {version!r}; this build reads {SCHEMA_VERSION!r}."
        )
    try:
        source_kind = SourceKind(payload["source_kind"])
        created_at = datetime.fromisoformat(payload["created_at"])
        ingested_at = datetime.fromisoformat(payload["ingested_at"])
    except (KeyError, ValueError) as exc:
        raise SnapshotError("manifest.json is missing required metadata.") from exc
    files = payload.get("files")
    if not isinstance(files, dict) or any(not isinstance(v, str) for v in files.values()):
        raise SnapshotError("manifest files must map relative paths to SHA-256 hex digests.")
    return SnapshotManifest(
        schema_version=version,
        dataset_id=str(payload["dataset_id"]),
        revision=str(payload["revision"]),
        source_kind=source_kind,
        created_at=created_at,
        ingested_at=ingested_at,
        files={str(key): str(value) for key, value in files.items()},
        symbols=tuple(payload.get("symbols", ())),
        counts={str(key): int(value) for key, value in payload.get("counts", {}).items()},
        vendor=payload.get("vendor"),
        metadata=dict(payload.get("metadata") or {}),
    )


def _verify_files(path: Path, manifest: SnapshotManifest) -> None:
    listed = set(manifest.files)
    present: set[str] = set()
    for child in path.rglob("*"):
        if not child.is_file():
            continue
        rel = child.relative_to(path).as_posix()
        if rel == MANIFEST_NAME:
            continue
        present.add(rel)
    extra = present - listed
    missing = listed - present
    if extra or missing:
        raise SnapshotIntegrityError(
            f"snapshot files do not match manifest "
            f"(extra={sorted(extra)} missing={sorted(missing)})"
        )
    for rel, expected in manifest.files.items():
        if ".." in Path(rel).parts or rel.startswith("/") or "\\" in rel:
            raise SnapshotIntegrityError(f"illegal snapshot path {rel!r}")
        digest = _sha256(path / rel)
        if digest != expected:
            raise SnapshotIntegrityError(f"SHA-256 mismatch for {rel}")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InvalidInputError(f"malformed JSON in {path.name}") from exc
    if not isinstance(payload, dict):
        raise InvalidInputError(f"{path.name} must contain a JSON object.")
    return payload


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise InvalidInputError(f"malformed JSONL in {path.name} line {line_no}") from exc
        if not isinstance(row, dict):
            raise InvalidInputError(f"{path.name} line {line_no} must be a JSON object.")
        rows.append(row)
    return rows


def _read_payload(path: Path, manifest: SnapshotManifest) -> SnapshotPayload:
    provenance = Provenance(
        source_kind=manifest.source_kind,
        dataset_id=manifest.dataset_id,
        revision=manifest.revision,
        vendor=manifest.vendor,
    )
    bars: list[MarketBarRecord] = []
    filings: list[FilingRecord] = []
    news: list[NewsRecord] = []
    statements: list[StatementRecord] = []
    research: list[ResearchArtifact] = []
    predictions: list[ReturnPrediction] = []
    ensembles: list[EnsemblePrediction] = []
    facts: list[XbrlFactRecord] = []
    vendor_raw: list[dict[str, Any]] = []
    parsers = {
        "bars": lambda file_path: bars.extend(
            parse_bar(row, provenance, require_ingested=True) for row in _load_jsonl(file_path)
        ),
        "filings": lambda file_path: filings.append(
            parse_filing(_load_json(file_path), provenance, require_ingested=True)
        ),
        "news": lambda file_path: news.append(
            parse_news(_load_json(file_path), provenance, require_ingested=True)
        ),
        "statements": lambda file_path: statements.append(
            parse_statement(_load_json(file_path), provenance, require_ingested=True)
        ),
        "research": lambda file_path: research.append(
            parse_research(_load_json(file_path), provenance, require_ingested=True)
        ),
        "predictions": lambda file_path: predictions.append(
            parse_prediction(_load_json(file_path), provenance, require_ingested=True)
        ),
        "ensembles": lambda file_path: ensembles.append(
            parse_ensemble(_load_json(file_path), provenance, require_ingested=True)
        ),
        "facts": lambda file_path: facts.extend(
            parse_fact(row, provenance, require_ingested=True) for row in _load_jsonl(file_path)
        ),
        "vendor": lambda file_path: vendor_raw.extend(_load_jsonl(file_path)),
    }
    for rel in sorted(manifest.files):
        kind = rel.split("/", 1)[0]
        parser = parsers.get(kind)
        if parser is None:
            raise UnsupportedSchemaError(f"unsupported snapshot object kind {kind!r}")
        parser(path / rel)
    return SnapshotPayload(
        bars=tuple(bars),
        filings=tuple(filings),
        news=tuple(news),
        statements=tuple(statements),
        research=tuple(research),
        predictions=tuple(predictions),
        ensembles=tuple(ensembles),
        facts=tuple(facts),
        vendor_raw=tuple(vendor_raw),
    )
