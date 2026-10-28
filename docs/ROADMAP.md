# ATLAS 로드맵

전체를 한 번에 구현하지 않는다. 각 마일스톤은 기존 `PaperBroker` / `RiskManager` / `safety`를 유지한 채 앞에 계층을 쌓는다.

## 마일스톤 0 — 완료에 가까움 (안전 골격)

이미 있다: src 레이아웃, 페이퍼 전용 설정, 확정적 리스크, 시뮬 시세, 호출자 제공 뉴스/재무, 휴리스틱 시그널, 바 리플레이 백테스트, CLI status.

남은 정리: 제품 비전 문서, 안정 데이터 계약 (`atlas.contracts`), 이벤트 시계의 **설계·타입** (본 작업).

## 마일스톤 1 — 완료 (PIT 백테스트 + 로컬 픽스처)

코드: `PitBacktestEngine`, `LocalFixtureSource`, `EventClock` 뷰, JSONL 저널. 기존 `BacktestEngine`은 동일 봉 종가로 시그널과 체결을 맞추므로 **연구 백테스트에 사용하면 안 된다** (`LookaheadBiasWarning`).

**목표:** 포인트-인-타임 이벤트 시계와 **읽기 전용** 로컬 적재. 네트워크 벤더와 LLM은 아직 없다.

### 1A. 이벤트 시계

- `TimeIndex` + `StreamEvent`로 바·뉴스·공시·거시 이벤트를 `(available_time, event_time, event_id)` 순으로 정렬한다.
- `EventClock.visible(as_of)`는 `available_time <= as_of`인 이벤트만 반환한다.
- 새 백테스트 루프(기존 `BacktestEngine`을 당장 삭제하지 않음):
  1. 시계를 `available_time`으로 진행한다.
  2. 시그널은 **이미 가시적인** 이벤트만 사용한다.
  3. 체결은 **다음 거래 가능 이벤트**(권장: 다음 봉 `open`)에서 `PaperBroker.submit(..., fill_price=open)` 한다.
  4. 같은 봉 `close`로 시그널과 체결을 맞추지 않는다 (룩어헤드 제거).
- 회귀 테스트: 미래 공시가 과거 시점에 보이면 실패, 다음 봉 체결가 사용.

### 1B. 읽기 전용 적재 (로컬 먼저)

- `ReadOnlyBarSource` / `ReadOnlyEventSource` 프로토콜만 구현한다.
- 첫 백엔드: 저장소 안 **픽스처 파일** (명시적 `simulated` 또는 `caller_supplied`).
- 심볼 정규화, OHLC, timezone, provenance 검증은 적재 시점에 수행한다.
- HTTP·API 키·스크래핑은 이 마일스톤에 넣지 않는다. 벤더 어댑터는 프로토콜 구현체로 **나중에** 붙인다.

### 1C. 감사 최소치

- 백테스트 런 ID, 사용한 이벤트 ID 목록, `RiskDecision.decision_id`, `PaperFill`을 한 구조로 남긴다.
- 디스크 영속화는 JSONL 한 파일이면 충분하다. DB는 나중.

### 완료 조건

- 픽스처 바 + 고의적 미래 누수 데이터가 있을 때 전략이 미래를 보지 못한다.
- 기존 pytest (페이퍼 안전)가 그대로 통과한다.
- 라이브 브로커 코드가 없다.

### 하지 말 것 (M1)

- 실계좌, 주문 라우팅
- 클라우드 LLM, 뉴스/공시 HTTP
- pandas 대량 도입이 꼭 필요하지 않으면 표준 라이브러리 유지
- 기존 `PaperBroker` 재작성

## 마일스톤 2 — 읽기 전용 실데이터 (설계: `docs/M2_READ_ONLY_INGESTION.md`)

### M2-A — 완료: 불변 스냅샷 저장소

`SnapshotStore`, `manifest.json`, SHA-256, 원자적 리비전 생성, `PitReplaySource`. 스키마: `docs/SNAPSHOT_SCHEMA.md`.

### M2-B — 구현 중: Alpaca 과거 시세 (모킹 HTTP)

`AlpacaBarsAdapter`, NYSE RTH 캘린더, `$10,000` 페이퍼 데모. 실제 `data.alpaca.markets` 호출은 `ATLAS_ALLOW_NETWORK`와 사람 승인이 필요하다.

사람 승인 후에만 HTTP를 연다. 이후 요지:

- SEC EDGAR 공시·제표와 Alpaca **과거 시세**의 GET 전용 어댑터 (`SourceKind.VENDOR_READ_ONLY`).
- `TimeIndex` + provenance, `dataset_id`/`revision` 스냅샷, 오프라인 픽스처·모킹 HTTP 테스트.
- 자격 증명은 환경 변수만, 로그 금지. 레이트 리밋. 쓰기는 없음.
- 페이퍼 전용 유지. LLM 에이전트와 정량 ML은 이후 마일스톤의 소비자.

## 마일스톤 3 — 리서치 에이전트

- `ResearchArtifact`만 산출하는 LLM 워커. 주문 객체 생성 금지.
- 영향 종목, 이벤트 요약, 근거 span, 모델 버전.
- 오프라인 픽스처 응답으로 먼저 테스트하고, 실제 LLM은 승인 후.

## 마일스톤 4 — 정량 예측과 결합

- 피처는 PIT 조인만 사용 (공시 `available_time` 이전 시세만).
- `ReturnPrediction`: 기대 수익, 불확실성, horizon, 모델 버전.
- `EnsemblePrediction`으로 여러 모델 결합. 휴리스틱 `ForecastEngine`은 벤치마크 베이스라인으로 유지.

## 마일스톤 5 — 포트폴리오 최적화

- 예측 + 공분산/불확실성 → 목표 가중치.
- 제약: 기존 `RiskLimits`를 확장 (이름목, 종목 집중, 회전율). 최적화기는 제안만, 체결은 여전히 `RiskManager` → `PaperBroker`.

## 마일스톤 6 — 페이퍼 운영 루프

- 스케줄된 이벤트 소비, 페이퍼 집행, 성과·예측 오차 대시보드(로컬).
- `decision_id` 레지스트리 영속화 (프로세스 재시작 후에도 거절 ID 재사용 금지).

## 마일스톤 7 — 실거래는 미정

별도 설계 리뷰, 규제·운영 승인, 장기간 페이퍼 실적 없이 착수하지 않는다. 코드에 LIVE 실행 경로를 미리 넣지 않는다.

## 권장 작업 순서 (즉시)

1. `atlas.contracts` 타입을 적재·백테스트 입력으로 쓰기 시작 (M1A).
2. `EventClock`으로 픽스처 스트림을 재현 가능하게 재생.
3. `BacktestEngine` **옆에** PIT 엔진을 추가하고, 구 엔진은 deprecated 안내만.
4. 그 다음에만 벤더 읽기 어댑터 설계 리뷰를 요청.
