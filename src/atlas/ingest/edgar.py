"""Read-only SEC EDGAR filings and companyfacts. No order routing."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Any
from urllib.parse import urlparse

import httpx

from atlas.contracts import (
    FilingRecord,
    Provenance,
    SourceKind,
    StatementRecord,
    TimeIndex,
    XbrlFactRecord,
)
from atlas.ingest.http import (
    SEC_ALLOWED_HOSTS,
    SEC_DATA_HOST,
    SEC_WWW_HOST,
    ReadOnlyHttp,
)
from atlas.ingest.snapshot import SnapshotManifest, SnapshotPayload, SnapshotStore
from atlas.market_data.nyse import EASTERN
from atlas.validation import InvalidInputError, normalize_symbol, require_finite

ADAPTER_VERSION = "atlas.edgar.companyfacts.v2"
RETRIEVAL_MOCKED_HTTP = "mocked_http"
RETRIEVAL_VENDOR_GET = "vendor_get"
DEFAULT_SYMBOL = "AAPL"
DEFAULT_FORMS = ("10-K", "10-Q", "8-K")
DEFAULT_MAX_FILINGS = 40
DEFAULT_MAX_FACTS = 2_000
DEFAULT_MAX_REQUESTS = 8
_TICKERS_PATH = "/files/company_tickers.json"
_SUBMISSIONS_PATH = "/submissions/CIK{cik}.json"
_FACTS_PATH = "/api/xbrl/companyfacts/CIK{cik}.json"
_TAXONOMIES = ("us-gaap", "dei", "ifrs-full")
_CONCEPT_ALIASES: dict[str, tuple[str, ...]] = {
    "revenue": (
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
    ),
    "net_income": ("NetIncomeLoss",),
    "assets": ("Assets",),
    "liabilities": ("Liabilities",),
    "cash": ("CashAndCashEquivalentsAtCarryingValue",),
    "operating_cash_flow": ("NetCashProvidedByUsedInOperatingActivities",),
    "eps_diluted": ("EarningsPerShareDiluted",),
    "eps_basic": ("EarningsPerShareBasic",),
    "equity": ("StockholdersEquity",),
    "shares": ("CommonStockSharesOutstanding", "EntityCommonStockSharesOutstanding"),
}
_EXTRA_CONCEPTS = frozenset(
    {
        "GrossProfit",
        "OperatingIncomeLoss",
        "LiabilitiesCurrent",
        "AssetsCurrent",
        "LongTermDebt",
    }
)
_ALLOWED_CONCEPTS = (
    frozenset(name for names in _CONCEPT_ALIASES.values() for name in names) | _EXTRA_CONCEPTS
)
_USD = frozenset({"USD"})
_USD_SHARES = frozenset({"USD/shares"})
_SHARES = frozenset({"shares"})
_CONCEPT_UNITS: dict[str, frozenset[str]] = {
    "RevenueFromContractWithCustomerExcludingAssessedTax": _USD,
    "Revenues": _USD,
    "SalesRevenueNet": _USD,
    "NetIncomeLoss": _USD,
    "Assets": _USD,
    "Liabilities": _USD,
    "CashAndCashEquivalentsAtCarryingValue": _USD,
    "NetCashProvidedByUsedInOperatingActivities": _USD,
    "GrossProfit": _USD,
    "OperatingIncomeLoss": _USD,
    "LiabilitiesCurrent": _USD,
    "AssetsCurrent": _USD,
    "LongTermDebt": _USD,
    "StockholdersEquity": _USD,
    "EarningsPerShareDiluted": _USD_SHARES,
    "EarningsPerShareBasic": _USD_SHARES,
    "CommonStockSharesOutstanding": _SHARES,
    "EntityCommonStockSharesOutstanding": _SHARES,
}
_DURATION_CONCEPTS = frozenset(
    {
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "NetIncomeLoss",
        "NetCashProvidedByUsedInOperatingActivities",
        "GrossProfit",
        "OperatingIncomeLoss",
        "EarningsPerShareDiluted",
        "EarningsPerShareBasic",
    }
)


class MissingEdgarUserAgentError(InvalidInputError):
    """SEC fair-access User-Agent is missing."""


class EdgarIntegrityError(InvalidInputError):
    """EDGAR payload failed identity, timestamp, or bound checks."""


@dataclass(frozen=True, slots=True)
class EdgarQuery:
    symbol: str = DEFAULT_SYMBOL
    forms: tuple[str, ...] = DEFAULT_FORMS
    include_amendments: bool = True
    max_filings: int = DEFAULT_MAX_FILINGS
    max_facts: int = DEFAULT_MAX_FACTS
    max_requests: int = DEFAULT_MAX_REQUESTS
    cik: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        forms = tuple(item.strip().upper() for item in self.forms if item.strip())
        if not forms:
            raise InvalidInputError("at least one EDGAR form is required.")
        object.__setattr__(self, "forms", forms)
        if self.max_filings < 1 or self.max_facts < 1 or self.max_requests < 1:
            raise InvalidInputError("EDGAR query bounds must be positive.")
        if self.cik is not None:
            object.__setattr__(self, "cik", _pad_cik(self.cik))


@dataclass(frozen=True, slots=True)
class EdgarIngestResult:
    manifest: SnapshotManifest
    filings: tuple[FilingRecord, ...]
    statements: tuple[StatementRecord, ...]
    facts: tuple[XbrlFactRecord, ...]
    cik: str
    retrieval_mode: str
    authenticated_vendor_data: bool
    simulated: bool


def load_edgar_user_agent() -> str:
    raw = os.environ.get("ATLAS_EDGAR_USER_AGENT", "").strip()
    if not raw or " " not in raw:
        raise MissingEdgarUserAgentError(
            "ATLAS_EDGAR_USER_AGENT must identify the operator (name and contact) "
            "and must not be committed to git."
        )
    return raw


class EdgarAdapter:
    """GET company tickers, submissions, and companyfacts from SEC hosts."""

    def __init__(
        self,
        *,
        http: ReadOnlyHttp | None = None,
        allow_network: bool | None = None,
        user_agent: str | None = None,
    ) -> None:
        self._user_agent = user_agent or load_edgar_user_agent()
        self.http = http or ReadOnlyHttp(
            allowed_hosts=SEC_ALLOWED_HOSTS,
            allow_network=allow_network,
            rate_limit_sleep=0.15,
            timeout_seconds=30.0,
            max_retries=3,
            user_agent=self._user_agent,
        )
        self._requests = 0
        self._max_requests = DEFAULT_MAX_REQUESTS

    def ingest(
        self,
        store: SnapshotStore,
        *,
        dataset_id: str,
        revision: str,
        query: EdgarQuery | None = None,
        created_at: datetime | None = None,
        retrieval_mode: str = RETRIEVAL_MOCKED_HTTP,
    ) -> EdgarIngestResult:
        query = query or EdgarQuery()
        if retrieval_mode not in {RETRIEVAL_MOCKED_HTTP, RETRIEVAL_VENDOR_GET}:
            raise InvalidInputError("retrieval_mode must be mocked_http or vendor_get.")
        mocked = retrieval_mode == RETRIEVAL_MOCKED_HTTP
        ingested_at = created_at or datetime.now(UTC)
        self._requests = 0
        self._max_requests = query.max_requests
        tickers_url = f"https://{SEC_WWW_HOST}{_TICKERS_PATH}"
        tickers_payload: Any = None
        if query.cik:
            cik = query.cik
        else:
            cik, tickers_payload = self._lookup_cik(query.symbol)
        submissions_url = f"https://{SEC_DATA_HOST}{_SUBMISSIONS_PATH.format(cik=cik)}"
        facts_url = f"https://{SEC_DATA_HOST}{_FACTS_PATH.format(cik=cik)}"
        submissions = self._get_json(submissions_url)
        facts_payload = self._get_json(facts_url)
        provenance = Provenance(
            source_kind=SourceKind.SIMULATED if mocked else SourceKind.VENDOR_READ_ONLY,
            dataset_id=dataset_id,
            revision=revision,
            vendor="sec_edgar",
        )
        index = _index_submissions(submissions)
        filings = _filings_from_submissions(query, cik, index, provenance, ingested_at)
        facts, fact_coverage = _facts_from_companyfacts(
            query, cik, facts_payload, index, provenance, ingested_at
        )
        statements, statement_gaps = _statements_from_facts(query.symbol, cik, facts, provenance)
        source_urls = {
            "submissions": submissions_url,
            "companyfacts": facts_url,
        }
        if tickers_payload is not None:
            source_urls["tickers"] = tickers_url
        metadata = _ingest_metadata(
            query,
            cik=cik,
            mocked=mocked,
            ingested_at=ingested_at,
            filings=filings,
            facts=facts,
            fact_coverage=fact_coverage,
            statement_gaps=statement_gaps,
            source_urls=source_urls,
        )
        vendor_items: list[dict[str, Any]] = []
        if tickers_payload is not None:
            vendor_items.append(
                {
                    "artifact": "company_tickers",
                    "url": tickers_url,
                    "host": SEC_WWW_HOST,
                    "body": tickers_payload,
                }
            )
        vendor_items.extend(
            (
                {
                    "artifact": "submissions",
                    "url": submissions_url,
                    "host": SEC_DATA_HOST,
                    "body": submissions,
                },
                {
                    "artifact": "companyfacts",
                    "url": facts_url,
                    "host": SEC_DATA_HOST,
                    "body": facts_payload,
                },
            )
        )
        vendor_raw = tuple(vendor_items)
        dest = store.revision_path(dataset_id, revision)
        try:
            manifest = store.create(
                dataset_id,
                revision,
                SnapshotPayload(
                    filings=tuple(filings),
                    statements=tuple(statements),
                    facts=tuple(facts),
                    vendor_raw=vendor_raw,
                ),
                source_kind=provenance.source_kind,
                vendor="sec_edgar",
                created_at=ingested_at,
                metadata=metadata,
            )
        except Exception:
            if dest.exists() and not (dest / "manifest.json").is_file():
                shutil_rmtree(dest)
            raise
        return EdgarIngestResult(
            manifest=manifest,
            filings=tuple(filings),
            statements=tuple(statements),
            facts=tuple(facts),
            cik=cik,
            retrieval_mode=retrieval_mode,
            authenticated_vendor_data=not mocked,
            simulated=mocked,
        )

    def _lookup_cik(self, symbol: str) -> tuple[str, Any]:
        payload = self._get_json(f"https://{SEC_WWW_HOST}{_TICKERS_PATH}")
        if not isinstance(payload, dict):
            raise EdgarIntegrityError("company_tickers.json must be a JSON object.")
        needle = symbol.upper()
        for row in payload.values():
            if not isinstance(row, dict):
                continue
            if str(row.get("ticker", "")).upper() == needle:
                return _pad_cik(row.get("cik_str", row.get("cik"))), payload
        raise EdgarIntegrityError(f"ticker {symbol!r} is not present in SEC company_tickers.")

    def _get_json(self, url: str) -> Any:
        self._requests += 1
        if self._requests > self._max_requests:
            raise EdgarIntegrityError(f"EDGAR ingest exceeded max_requests={self._max_requests}.")
        parsed = urlparse(url)
        if parsed.hostname not in SEC_ALLOWED_HOSTS:
            raise EdgarIntegrityError(f"host {parsed.hostname!r} is not an SEC data host.")
        response = self.http.get(url, headers={"User-Agent": self._user_agent})
        payload = response.json()
        return payload

    def _auth_headers(self) -> dict[str, str]:
        return {"User-Agent": self._user_agent}


def mock_edgar_client(handler: Any) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url=f"https://{SEC_DATA_HOST}",
        follow_redirects=False,
    )


def adapter_for_tests(
    handler: Any, *, user_agent: str = "ATLAS tests contact@example.com"
) -> EdgarAdapter:
    http = ReadOnlyHttp(
        allowed_hosts=SEC_ALLOWED_HOSTS,
        client=mock_edgar_client(handler),
        allow_network=False,
        rate_limit_sleep=0.0,
        sleeper=lambda _delay: None,
        user_agent=user_agent,
    )
    return EdgarAdapter(http=http, user_agent=user_agent)


def shutil_rmtree(path: Any) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


def _pad_cik(value: Any) -> str:
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if not digits:
        raise EdgarIntegrityError("CIK must contain digits.")
    return digits.zfill(10)


def _index_submissions(payload: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    recent = (payload.get("filings") or {}).get("recent") or {}
    accessions = recent.get("accessionNumber") or []
    forms = recent.get("form") or []
    report_dates = recent.get("reportDate") or []
    filed_dates = recent.get("filingDate") or []
    accepted = recent.get("acceptanceDateTime") or []
    documents = recent.get("primaryDocument") or []
    items = recent.get("items") or []
    headers = recent.get("headerAcceptanceDateTime") or []
    length = len(accessions)
    if not length:
        return {}
    for name, column in (
        ("form", forms),
        ("filingDate", filed_dates),
        ("acceptanceDateTime", accepted),
    ):
        if len(column) != length:
            raise EdgarIntegrityError(f"submissions recent.{name} length mismatch.")
    index: dict[str, dict[str, Any]] = {}
    for i, accession in enumerate(accessions):
        accn = str(accession).strip()
        if not accn:
            continue
        index[accn] = {
            "form": str(forms[i]).strip(),
            "reportDate": _optional_row(report_dates, i),
            "filingDate": str(filed_dates[i]).strip(),
            "acceptanceDateTime": str(accepted[i]).strip(),
            "primaryDocument": _optional_row(documents, i),
            "items": _optional_row(items, i),
            "headerAcceptanceDateTime": _optional_row(headers, i),
        }
    return index


def _optional_row(column: Sequence[Any], index: int) -> str:
    if index >= len(column):
        return ""
    return str(column[index] or "").strip()


def _filings_from_submissions(
    query: EdgarQuery,
    cik: str,
    index: Mapping[str, Mapping[str, Any]],
    provenance: Provenance,
    ingested_at: datetime,
) -> list[FilingRecord]:
    allowed = {form.upper() for form in query.forms}
    records: list[FilingRecord] = []
    for accession, row in index.items():
        form = str(row["form"])
        if not _form_allowed(form, allowed, query.include_amendments):
            continue
        stamp = _availability_from_submission(row)
        period = _parse_day(str(row.get("reportDate") or ""), "reportDate")
        if _is_current_event_form(form) or period is None:
            event_time = stamp.available
        else:
            event_time = datetime.combine(period, time.min, tzinfo=UTC)
        if stamp.available < event_time:
            raise EdgarIntegrityError(
                f"acceptance {stamp.available.isoformat()} precedes event {event_time.isoformat()}."
            )
        records.append(
            FilingRecord(
                symbol=query.symbol,
                form_type=form,
                time=TimeIndex(
                    event_time=event_time,
                    available_time=stamp.available,
                    ingested_time=ingested_at,
                ),
                provenance=provenance,
                accession=accession,
                cik=cik,
                primary_document=str(row.get("primaryDocument") or "") or None,
                items=_split_items(str(row.get("items") or "")),
                is_amendment=form.endswith("/A"),
                raw_acceptance=stamp.raw,
                acceptance_verified=stamp.verified,
                intraday_eligible=stamp.intraday_eligible,
            )
        )
    records.sort(key=lambda item: (item.time.available_time, item.accession), reverse=True)
    if len(records) > query.max_filings:
        records = records[: query.max_filings]
    records.sort(key=lambda item: (item.time.available_time, item.accession))
    return records


def _facts_from_companyfacts(
    query: EdgarQuery,
    cik: str,
    payload: Mapping[str, Any],
    index: Mapping[str, Mapping[str, Any]],
    provenance: Provenance,
    ingested_at: datetime,
) -> tuple[list[XbrlFactRecord], dict[str, Any]]:
    facts_root = payload.get("facts")
    if not isinstance(facts_root, dict):
        raise EdgarIntegrityError("companyfacts.facts must be an object.")
    allowed_forms = {form.upper() for form in query.forms}
    kept: list[XbrlFactRecord] = []
    seen: set[str] = set()
    unit_mismatch = 0
    skipped_invalid = 0
    for taxonomy in _TAXONOMIES:
        concepts = facts_root.get(taxonomy)
        if not isinstance(concepts, dict):
            continue
        for concept in sorted(concepts):
            if concept not in _ALLOWED_CONCEPTS:
                continue
            body = concepts[concept]
            units = (body or {}).get("units") or {}
            if not isinstance(units, dict):
                continue
            expected_units = _CONCEPT_UNITS.get(concept)
            for unit in sorted(units):
                rows = units[unit]
                if not isinstance(rows, list):
                    continue
                if expected_units is not None and unit not in expected_units:
                    unit_mismatch += len(rows)
                    continue
                for row in rows:
                    if not isinstance(row, dict):
                        skipped_invalid += 1
                        continue
                    form = str(row.get("form") or "").strip()
                    if form and not _form_allowed(form, allowed_forms, query.include_amendments):
                        continue
                    accession = str(row.get("accn") or "").strip()
                    if not accession:
                        skipped_invalid += 1
                        continue
                    end_raw = row.get("end") or row.get("instant")
                    start_day = _parse_day(str(row.get("start") or ""), "fact start")
                    event_day = _parse_day(str(end_raw or ""), "fact end")
                    if event_day is None:
                        skipped_invalid += 1
                        continue
                    if concept in _DURATION_CONCEPTS and start_day is None:
                        skipped_invalid += 1
                        continue
                    event_time = datetime.combine(event_day, time.min, tzinfo=UTC)
                    period_start = (
                        datetime.combine(start_day, time.min, tzinfo=UTC) if start_day else None
                    )
                    period_kind = classify_period_kind(
                        start_day, event_day, str(row.get("fp") or ""), str(row.get("frame") or "")
                    )
                    stamp = _fact_availability(row, accession, index)
                    if stamp.available < event_time:
                        skipped_invalid += 1
                        continue
                    try:
                        value = require_finite(row.get("val"), "val")
                    except InvalidInputError:
                        skipped_invalid += 1
                        continue
                    record = XbrlFactRecord(
                        symbol=query.symbol,
                        cik=cik,
                        concept=str(concept),
                        taxonomy=taxonomy,
                        unit=str(unit),
                        value=value,
                        time=TimeIndex(
                            event_time=event_time,
                            available_time=stamp.available,
                            ingested_time=ingested_at,
                        ),
                        provenance=provenance,
                        accession=accession,
                        form_type=form or None,
                        fiscal_year=_optional_int(row.get("fy")),
                        fiscal_period=str(row.get("fp") or "") or None,
                        frame=str(row.get("frame") or "") or None,
                        period_start=period_start,
                        period_kind=period_kind,
                        raw_acceptance=stamp.raw,
                        acceptance_verified=stamp.verified,
                        intraday_eligible=stamp.intraday_eligible,
                    )
                    if record.ref_id in seen:
                        continue
                    seen.add(record.ref_id)
                    kept.append(record)
    kept.sort(key=_fact_sort_key)
    truncated = len(kept) > query.max_facts
    omitted = kept[query.max_facts :] if truncated else []
    selected = kept[: query.max_facts]
    coverage = {
        "candidate_facts": len(kept),
        "stored_facts": len(selected),
        "omitted_facts": len(omitted),
        "truncated": truncated,
        "selection": (
            "Deterministic newest-first by available_time, then concept, taxonomy, unit, "
            "period, accession. Truncation prefers recent observations, not first-seen JSON order."
        ),
        "unit_mismatch_rows": unit_mismatch,
        "skipped_invalid_rows": skipped_invalid,
        "omitted_concepts": sorted({item.concept for item in omitted}),
    }
    return selected, coverage


def _statements_from_facts(
    symbol: str,
    cik: str,
    facts: Sequence[XbrlFactRecord],
    provenance: Provenance,
) -> tuple[list[StatementRecord], list[dict[str, Any]]]:
    statements: list[StatementRecord] = []
    gaps: list[dict[str, Any]] = []
    duration_eps = [
        fact
        for fact in facts
        if fact.concept in _CONCEPT_ALIASES["eps_diluted"] + _CONCEPT_ALIASES["eps_basic"]
        and fact.period_kind in {"duration_annual", "duration_quarter"}
    ]
    instants = [fact for fact in facts if fact.period_kind == "instant"]
    durations = [
        fact
        for fact in facts
        if fact.period_kind in {"duration_annual", "duration_quarter", "duration_ytd"}
    ]
    for eps_fact in duration_eps:
        end = eps_fact.time.event_time.date()
        start = eps_fact.period_start.date() if eps_fact.period_start is not None else None
        equity = _match_instant(instants, eps_fact.accession, end, _CONCEPT_ALIASES["equity"])
        shares = _match_instant(instants, eps_fact.accession, end, _CONCEPT_ALIASES["shares"])
        if equity is None or shares is None or shares.value == 0:
            gaps.append(
                {
                    "accession": eps_fact.accession,
                    "period_end": end.isoformat(),
                    "period_kind": eps_fact.period_kind,
                    "missing": [
                        name
                        for name, present in (("equity", equity), ("shares", shares))
                        if present is None
                    ],
                    "reason": "incomplete_ratio_inputs",
                }
            )
            continue
        metrics: list[tuple[str, float]] = []
        for label, names in _CONCEPT_ALIASES.items():
            if label in {"eps_diluted", "eps_basic", "equity", "shares"}:
                continue
            if label in {"assets", "liabilities", "cash"}:
                match = _match_instant(instants, eps_fact.accession, end, names)
            else:
                match = _match_duration(
                    durations, eps_fact.accession, start, end, eps_fact.period_kind, names
                )
            if match is not None:
                metrics.append((label, match.value))
        statements.append(
            StatementRecord(
                symbol=symbol,
                time=eps_fact.time,
                provenance=provenance,
                earnings_per_share=eps_fact.value,
                book_value_per_share=equity.value / shares.value,
                metrics=tuple(metrics),
                cik=cik,
                accession=eps_fact.accession,
                form_type=eps_fact.form_type,
                period_kind=eps_fact.period_kind,
                period_start=eps_fact.period_start,
            )
        )
    return statements, gaps


def _match_instant(
    facts: Sequence[XbrlFactRecord],
    accession: str,
    end: date,
    names: Sequence[str],
) -> XbrlFactRecord | None:
    wanted = set(names)
    for fact in facts:
        if (
            fact.accession == accession
            and fact.concept in wanted
            and fact.time.event_time.date() == end
        ):
            return fact
    return None


def _match_duration(
    facts: Sequence[XbrlFactRecord],
    accession: str,
    start: date | None,
    end: date,
    period_kind: str,
    names: Sequence[str],
) -> XbrlFactRecord | None:
    wanted = set(names)
    for fact in facts:
        fact_start = fact.period_start.date() if fact.period_start is not None else None
        if (
            fact.accession == accession
            and fact.concept in wanted
            and fact.time.event_time.date() == end
            and fact_start == start
            and fact.period_kind == period_kind
        ):
            return fact
    return None


def _fact_sort_key(fact: XbrlFactRecord) -> tuple[Any, ...]:
    start = fact.period_start.isoformat() if fact.period_start is not None else ""
    return (
        -fact.time.available_time.timestamp(),
        fact.concept,
        fact.taxonomy,
        fact.unit,
        start,
        fact.time.event_time.isoformat(),
        fact.accession,
    )


def _form_allowed(form: str, allowed: set[str], include_amendments: bool) -> bool:
    candidate = form.upper()
    if candidate in allowed:
        return True
    if include_amendments and candidate.endswith("/A") and candidate[:-2] in allowed:
        return True
    return False


def _is_current_event_form(form: str) -> bool:
    base = form.upper()[:-2] if form.upper().endswith("/A") else form.upper()
    return base == "8-K"


def _split_items(raw: str) -> tuple[str, ...]:
    if not raw.strip():
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


@dataclass(frozen=True, slots=True)
class _Availability:
    available: datetime
    raw: str | None
    verified: bool
    intraday_eligible: bool


def ny_end_of_day(day: date) -> datetime:
    """End of the America/New_York calendar date, as UTC. Not 23:59 UTC."""
    local = datetime.combine(day, time(23, 59, 59), tzinfo=EASTERN)
    return local.astimezone(UTC)


def classify_period_kind(start: date | None, end: date, fiscal_period: str, frame: str) -> str:
    if start is None:
        return "instant"
    days = (end - start).days + 1
    fp = fiscal_period.upper()
    frame_u = frame.upper()
    if fp == "FY" or days >= 300 or (frame_u.startswith("CY") and "Q" not in frame_u):
        return "duration_annual"
    if days > 100:
        return "duration_ytd"
    if fp in {"Q1", "Q2", "Q3"} or days <= 100:
        return "duration_quarter"
    return "duration_other"


def parse_sec_timestamp(raw: str) -> _Availability:
    text = raw.strip()
    if not text:
        raise EdgarIntegrityError("timestamp is empty.")
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        day = _parse_day(text, "filed")
        if day is None:
            raise EdgarIntegrityError("date-only timestamp is invalid.")
        return _Availability(ny_end_of_day(day), text, False, False)
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise EdgarIntegrityError("timestamp must be ISO-8601.") from exc
    if parsed.tzinfo is None:
        return _Availability(ny_end_of_day(parsed.date()), text, False, False)
    return _Availability(parsed.astimezone(UTC), text, True, True)


def _availability_from_submission(row: Mapping[str, Any]) -> _Availability:
    raw = str(row.get("acceptanceDateTime") or "").strip()
    header = str(row.get("headerAcceptanceDateTime") or "").strip()
    if not raw:
        filed = str(row.get("filingDate") or "").strip()
        return parse_sec_timestamp(filed)
    stamp = parse_sec_timestamp(raw)
    if header:
        header_stamp = parse_sec_timestamp(header)
        if (
            header_stamp.verified
            and stamp.verified
            and abs((header_stamp.available - stamp.available).total_seconds()) > 1
        ):
            later = max(header_stamp.available, stamp.available)
            return _Availability(later, raw, False, False)
    return stamp


def _fact_availability(
    row: Mapping[str, Any], accession: str, index: Mapping[str, Mapping[str, Any]]
) -> _Availability:
    meta = index.get(accession)
    if meta:
        return _availability_from_submission(meta)
    filed = str(row.get("filed") or "").strip()
    if not filed:
        raise EdgarIntegrityError("fact is missing filed/acceptance time.")
    return parse_sec_timestamp(filed)


def _parse_day(raw: str, name: str) -> date | None:
    if not raw.strip():
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError as exc:
        raise EdgarIntegrityError(f"{name} must be YYYY-MM-DD.") from exc


def _optional_int(value: Any) -> int | None:
    if value in {None, ""}:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _ingest_metadata(
    query: EdgarQuery,
    *,
    cik: str,
    mocked: bool,
    ingested_at: datetime,
    filings: Sequence[FilingRecord],
    facts: Sequence[XbrlFactRecord],
    fact_coverage: Mapping[str, Any],
    statement_gaps: Sequence[Mapping[str, Any]],
    source_urls: Mapping[str, str],
) -> dict[str, Any]:
    if mocked:
        disclaimer = "Mocked HTTP fixture. Not authenticated SEC EDGAR data and not a live filing."
    else:
        disclaimer = "Read-only SEC EDGAR historical GET. Not investment advice."
    return {
        "vendor": "sec_edgar",
        "adapter_version": ADAPTER_VERSION,
        "retrieval_mode": RETRIEVAL_MOCKED_HTTP if mocked else RETRIEVAL_VENDOR_GET,
        "authenticated_vendor_data": not mocked,
        "symbol": query.symbol,
        "cik": cik,
        "forms": list(query.forms),
        "include_amendments": query.include_amendments,
        "hosts": sorted(SEC_ALLOWED_HOSTS),
        "method": "GET",
        "paths": {
            "tickers": _TICKERS_PATH,
            "submissions": _SUBMISSIONS_PATH.format(cik=cik),
            "companyfacts": _FACTS_PATH.format(cik=cik),
        },
        "source_urls": dict(source_urls),
        "ingested_at": ingested_at.isoformat(),
        "filing_count": len(filings),
        "intraday_eligible_filings": sum(1 for item in filings if item.intraday_eligible),
        "fact_count": len(facts),
        "intraday_eligible_facts": sum(1 for item in facts if item.intraday_eligible),
        "fact_coverage": dict(fact_coverage),
        "incomplete_statements": list(statement_gaps),
        "query_bounds": {
            "max_filings": query.max_filings,
            "max_facts": query.max_facts,
            "max_requests": query.max_requests,
        },
        "price_disclaimer": disclaimer,
        "research_notes": (
            "M3 ResearchArtifact.evidence_refs may cite filing:{accession} and "
            "fact:{symbol}:{taxonomy}:{concept}:{unit}:{start}:{end}:{period_kind}:{accession}. "
            "M4 features must join on available_time, never on the latest ingested value. "
            "Unverified timestamps are not intraday_eligible."
        ),
    }
