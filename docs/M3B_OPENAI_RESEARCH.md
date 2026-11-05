# M3-B — OpenAI evidence-grounded research

Default remains **offline**. This milestone adds an opt-in OpenAI Responses provider. It does not place trades and does not call the API unless a human sets `ATLAS_ALLOW_LLM_NETWORK=1`.

## Timestamps

| Field | Meaning |
| --- | --- |
| `research_as_of` | Evidence cutoff. Only snapshot rows with `available_time <= research_as_of` are sent. |
| `generated_at` | When the model (or mock) finished. |
| `time.event_time` | Research generation event (`generated_at`). |
| `time.available_time` | `max(generated_at, research_as_of)`. Never backdated to the filing date. |
| `time.ingested_time` | Durable snapshot write time. |

A filing from July cannot make today's research look available in July. `--retrospective` marks replay analyses and sets `usable_for_signals=false` so they cannot enter past trading views.

## Validation

Quotes must sit inside the cited chunk's cleaned-text range, match that substring, and name a retrieved evidence ID. XBRL `asserted_value` / unit / period / accession must match the referenced fact when provided. Passing these checks does **not** set `semantically_verified`.

Invalid, refused, incomplete, or budget-exceeded LLM output is not published to the normal research dataset.

## Provider

- `--provider mock` — deterministic, no HTTP (M3-A).
- `--provider openai` — OpenAI Python SDK `client.responses.parse`, Structured Outputs, `store=false`, no tools.
- Model id comes from `ATLAS_OPENAI_MODEL`. There is no built-in default model or price.
- Secret: `OPENAI_API_KEY`. Never log or commit it.
- Optional cost: set **both** `ATLAS_OPENAI_PRICE_INPUT_PER_MILLION` and `ATLAS_OPENAI_PRICE_OUTPUT_PER_MILLION`. Otherwise `estimated_cost` is omitted.

### Data sent on a live call

System instructions (`docs` / `atlas.research.prompts`) plus a JSON evidence pack: PIT-filtered chunk text, headings, hashes, offsets, and XBRL facts. No API keys, no brokerage data, no orders. OpenAI `store=false` is requested; the vendor may still process the payload under their policy.

## First real LLM call (not used in CI)

Do this only after an operator explicitly approves a paid request:

1. `uv run atlas research analyze ... --provider openai --dry-run` and read the estimated token count.
2. Copy `.env.example` to `.env`. Set `ATLAS_OPENAI_MODEL` to a Responses + Structured Outputs model you accept. Set `OPENAI_API_KEY`.
3. Set `ATLAS_ALLOW_LLM_NETWORK=1`.
4. Run `atlas research analyze` with a **new** `--dataset` / `--revision`. One request is the default budget.
5. Unset `ATLAS_ALLOW_LLM_NETWORK`. Rotate the key if it leaked.

`--dry-run` never opens a socket. Tests use a fake `responses.parse` client only.

## CLI

```text
atlas research analyze --root DIR --symbol AAPL \
  --document-dataset aapl-10q-doc-real --document-revision 2026-11-05-02 \
  --financial-dataset aapl-edgar-real --financial-revision 2026-10-28-02 \
  --provider openai --dry-run --accession 0000320193-26-000020
```
