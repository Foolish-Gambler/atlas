from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from atlas.backtesting import NextOpenBuyStrategy, PitBacktestEngine
from atlas.contracts import EventClock, SourceKind
from atlas.ingest.local import LocalFixtureSource
from atlas.ingest.snapshot import (
    RevisionExistsError,
    SnapshotIntegrityError,
    SnapshotPayload,
    SnapshotStore,
    UnsupportedSchemaError,
)
from atlas.validation import InvalidInputError

FIXTURE = Path(__file__).parent / "fixtures" / "pit" / "m1_sim_a.json"


def test_create_is_immutable_and_hashed(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path)
    created = datetime(2024, 1, 6, tzinfo=UTC)
    first = store.create_from_fixture(FIXTURE, created_at=created)
    assert first.schema_version == "1"
    assert first.dataset_id == "m1-sim-a"
    assert first.revision == "2"
    assert first.source_kind is SourceKind.SIMULATED
    assert first.files
    with pytest.raises(RevisionExistsError, match="immutable"):
        store.create_from_fixture(FIXTURE, created_at=created)
    verified = store.verify("m1-sim-a", "2")
    assert verified.files == first.files
    source = store.load("m1-sim-a", "2")
    assert source.all_bars()[0].ingested_time == created
    assert source.filings()[0].time.ingested_time == created
    assert source.filings()[0].time.event_time != source.filings()[0].time.available_time
    assert source.filings()[0].time.available_time <= source.filings()[0].time.ingested_time


def test_tampered_bytes_fail_hash_verification(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path)
    store.create_from_fixture(FIXTURE)
    bar_file = tmp_path / "m1-sim-a" / "2" / "bars" / "SIM-A.jsonl"
    bar_file.write_bytes(bar_file.read_bytes() + b" ")
    with pytest.raises(SnapshotIntegrityError, match="SHA-256"):
        store.verify("m1-sim-a", "2")
    with pytest.raises(SnapshotIntegrityError, match="SHA-256"):
        store.load("m1-sim-a", "2")


def test_extra_file_fails_integrity(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path)
    store.create_from_fixture(FIXTURE)
    (tmp_path / "m1-sim-a" / "2" / "bars" / "EXTRA.jsonl").write_text("{}\n", encoding="utf-8")
    with pytest.raises(SnapshotIntegrityError, match="extra"):
        store.verify("m1-sim-a", "2")


def test_failed_publish_leaves_no_revision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = SnapshotStore(tmp_path)

    original = Path.replace

    def selective_boom(self: Path, target: Path) -> Path:
        if Path(target).name == "2":
            raise OSError("simulated publish failure")
        return original(self, target)

    monkeypatch.setattr(Path, "replace", selective_boom)
    with pytest.raises(OSError, match="simulated publish"):
        store.create_from_fixture(FIXTURE)
    dataset_dir = tmp_path / "m1-sim-a"
    assert not (dataset_dir / "2").exists()
    if dataset_dir.exists():
        assert not any(path.name.startswith(".2.tmp-") for path in dataset_dir.iterdir())


def test_unsupported_schema_is_rejected(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path)
    store.create_from_fixture(FIXTURE)
    manifest_path = tmp_path / "m1-sim-a" / "2" / "manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["schema_version"] = "99"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(UnsupportedSchemaError, match="schema_version"):
        store.inspect("m1-sim-a", "2")


def test_malformed_bar_and_lookahead_are_rejected(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path)
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    bad = tmp_path / "bad.json"
    fixture["bars"][0]["available_time"] = "2024-01-02T10:00:00+00:00"
    bad.write_text(json.dumps(fixture), encoding="utf-8")
    with pytest.raises(InvalidInputError, match="available_time"):
        store.create_from_fixture(bad)

    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    fixture["bars"][0]["open"] = "not-a-price"
    bad.write_text(json.dumps(fixture), encoding="utf-8")
    with pytest.raises(InvalidInputError):
        store.create_from_fixture(bad)


def test_path_traversal_ids_are_rejected(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path)
    with pytest.raises(InvalidInputError, match="path-safe"):
        store.create_from_fixture(FIXTURE, dataset_id="../escape")


def test_snapshot_replay_matches_fixture_pit_engine(tmp_path: Path, settings) -> None:
    store = SnapshotStore(tmp_path)
    store.create_from_fixture(FIXTURE, created_at=datetime(2024, 1, 10, tzinfo=UTC))
    snapshot = store.load("m1-sim-a", "2")
    fixture = LocalFixtureSource(FIXTURE)
    snap_clock = EventClock(snapshot.stream())
    fixture_clock = EventClock(fixture.stream())
    assert [event.event_id for event in snap_clock.events] == [
        event.event_id for event in fixture_clock.events
    ]
    engine = PitBacktestEngine(settings=settings)
    from_snapshot = engine.run(snapshot, NextOpenBuyStrategy(quantity=1), run_id="from-snap")
    from_fixture = engine.run(fixture, NextOpenBuyStrategy(quantity=1), run_id="from-snap")
    assert from_snapshot.fills == from_fixture.fills
    assert from_snapshot.ending_cash == from_fixture.ending_cash
    assert from_snapshot.ending_equity == from_fixture.ending_equity
    assert from_snapshot.journal.dumps() == from_fixture.journal.dumps()


def test_empty_payload_is_allowed_to_create_but_replay_needs_bars(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path)
    manifest = store.create(
        "empty-set",
        "1",
        SnapshotPayload(),
        source_kind=SourceKind.SIMULATED,
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    )
    assert manifest.counts["bars"] == 0
    loaded = store.load("empty-set", "1")
    assert loaded.all_bars() == ()


def test_ingested_time_cannot_precede_available_time() -> None:
    from atlas.contracts import MarketBarRecord, Provenance, TimeIndex

    provenance = Provenance(source_kind=SourceKind.SIMULATED, dataset_id="x", revision="1")
    with pytest.raises(InvalidInputError, match="ingested_time"):
        TimeIndex(
            event_time=datetime(2024, 1, 2, tzinfo=UTC),
            available_time=datetime(2024, 1, 3, tzinfo=UTC),
            ingested_time=datetime(2024, 1, 2, 12, tzinfo=UTC),
        )
    with pytest.raises(InvalidInputError, match="ingested_time"):
        MarketBarRecord(
            symbol="SIM-A",
            open_time=datetime(2024, 1, 2, 14, 30, tzinfo=UTC),
            close_time=datetime(2024, 1, 2, 21, tzinfo=UTC),
            available_time=datetime(2024, 1, 2, 21, tzinfo=UTC),
            provenance=provenance,
            open=1.0,
            high=1.0,
            low=1.0,
            close=1.0,
            volume=1,
            ingested_time=datetime(2024, 1, 2, 20, tzinfo=UTC),
        )


def test_new_revision_does_not_mutate_previous(tmp_path: Path) -> None:
    store = SnapshotStore(tmp_path)
    store.create_from_fixture(FIXTURE, revision="r1", created_at=datetime(2024, 1, 6, tzinfo=UTC))
    later = datetime(2024, 1, 7, tzinfo=UTC)
    store.create_from_fixture(FIXTURE, revision="r2", created_at=later)
    first = store.load("m1-sim-a", "r1")
    second = store.load("m1-sim-a", "r2")
    assert first.all_bars()[0].ingested_time == datetime(2024, 1, 6, tzinfo=UTC)
    assert second.all_bars()[0].ingested_time == later
    assert first.manifest.revision == "r1"
