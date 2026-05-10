# LLM Post-Processing — Architecture & Design

The LLM normalisation pass is a Claude-powered quality layer that runs **inside the ECS worker** after Sarvam transcription and translation, and is also available as a **standalone Streamlit portal** (`postprocess-ui/`) for manual review.

The worker produces raw ASR output with speaker diarization. Claude fixes clinical terminology, restores romanised Indian-language text to native script, cleans formatting, and removes ASR noise — adding `normalized_transcript` and `normalized_translation` fields to every segment in the results JSON.

---

## Table of Contents

1. [What Problem It Solves](#what-problem-it-solves)
2. [Two Deployment Modes](#two-deployment-modes)
3. [System Overview](#system-overview)
4. [Data Flow](#data-flow)
5. [File Responsibilities](#file-responsibilities)
6. [Stage 1 — Segment Grouping](#stage-1--segment-grouping)
7. [Stage 2 — Batching](#stage-2--batching)
8. [Stage 3 — The Prompt](#stage-3--the-prompt)
9. [Stage 4 — Output Validation](#stage-4--output-validation)
10. [Stage 5 — Assemble](#stage-5--assemble)
11. [Glossary File](#glossary-file)
12. [Output Schema](#output-schema)
13. [Model Configuration](#model-configuration)
14. [Running Locally](#running-locally)

---

## What Problem It Solves

The raw pipeline output has four systematic failure modes:

| Problem | Example |
|---|---|
| Clinical term mishearing | "cat distributing" → should be "catastrophising" |
| Romanised Indian-language text | "ab dekho" → should be "अब देखो" (Devanagari) |
| Poor formatting | run-on sentences, no punctuation, no paragraph breaks |
| ASR noise | "I I I was thinking um um maybe" |

None of these are fixable at the ASR level. They require understanding the clinical domain, the language being spoken, and the conversational context. Claude handles all four in a single pass over each batch of turns.

---

## Two Deployment Modes

The same core logic runs in two contexts:

| Mode | Where | When to use |
|---|---|---|
| **Worker (automatic)** | `worker/src/pipeline/postprocess.py` | Every job — runs inline after Sarvam, before S3 write. Controlled by `POSTPROCESS_ENABLED` and `ANTHROPIC_SECRET_NAME`. Segments map 1:1 to turns (no grouping). |
| **Standalone UI (manual)** | `postprocess-ui/` | Upload an existing `results.json` for manual review — shows side-by-side original vs normalised diff, supports custom glossary input, download cleaned JSON. Segments are grouped into speaker turns before sending to Claude. |

In the worker, a normalisation failure is **never fatal** — it logs a warning and the job completes with empty `normalized_transcript` / `normalized_translation` fields. In the UI, errors are shown inline and you can retry.

---

## System Overview

```
User browser
    │
    │  upload  results.json  (from ECS pipeline)
    ▼
app.py  (Streamlit)
    │
    │  pipeline_run(raw, model, glossary)
    ▼
pipeline.py
    ├── group_turns()        merge consecutive same-speaker segments → turns
    ├── batch_turns()        split turns into ≤80K char batches
    └── for each batch:
            clean_batch(llm, batch, glossary)
                ├── build_system_prompt()    ──┐
                ├── build_user_message()    ──┤  prompt.py
                │                             ┘
                │  [SystemMessage + HumanMessage]
                ▼
            llm.py  →  ChatAnthropic  (claude-opus-4-6 or claude-sonnet-4-6)
                │
                ▼  CleanedTurns (validated Pydantic object)
                │
                ├── _align_to_batch()        fill any skipped turns with passthrough
                └── _merge_glossary()        accumulate corrections across batches
    │
    ├── assemble()           zip cleaned turns back onto original document
    ▼
enriched JSON  (original + postprocess key)
    │
    ▼
app.py  renders diff table, full document view, download button
```

---

## Data Flow

### Input — `results.json` from the ECS pipeline

```json
{
  "job_id": "abc-123",
  "segments": [
    { "speaker_id": 0, "start_time": 1.6,  "end_time": 3.2,  "transcription": "so um how are you feeling today",         "translation": "so um how are you feeling today" },
    { "speaker_id": 0, "start_time": 3.2,  "end_time": 5.1,  "transcription": "any changes since last week",              "translation": "any changes since last week" },
    { "speaker_id": 1, "start_time": 5.5,  "end_time": 9.8,  "transcription": "haan I I I feel like mood bahut low hai",  "translation": "yes I feel like mood is very low" },
    { "speaker_id": 1, "start_time": 9.8,  "end_time": 13.2, "transcription": "aur neend bhi nahi aati uh uh",            "translation": "and sleep also does not come" },
    { "speaker_id": 0, "start_time": 13.5, "end_time": 16.0, "transcription": "okay are you cat distributing again",      "translation": "okay are you cat distributing again" }
  ]
}
```

### Output — enriched JSON with `postprocess` key added

```json
{
  "job_id": "abc-123",
  "segments": [ ... ],
  "postprocess": {
    "model": "claude-opus-4-6",
    "turns": [
      {
        "turn_index": 0,
        "speaker_id": 0,
        "start_time": 1.6,
        "end_time": 5.1,
        "transcription":         "so um how are you feeling today any changes since last week",
        "translation":           "so um how are you feeling today any changes since last week",
        "cleaned_transcription": "How are you feeling today? Any changes since last week?",
        "cleaned_translation":   "How are you feeling today? Any changes since last week?"
      },
      {
        "turn_index": 1,
        "speaker_id": 1,
        "start_time": 5.5,
        "end_time": 13.2,
        "transcription":         "haan I I I feel like mood bahut low hai aur neend bhi nahi aati uh uh",
        "translation":           "yes I feel like mood is very low and sleep also does not come",
        "cleaned_transcription": "हाँ, I feel like mood बहुत low है, और नींद भी नहीं आती।",
        "cleaned_translation":   "Yes, I feel like my mood has been very low, and I'm not sleeping well either."
      },
      {
        "turn_index": 2,
        "speaker_id": 0,
        "start_time": 13.5,
        "end_time": 16.0,
        "transcription":         "okay are you cat distributing again",
        "translation":           "okay are you cat distributing again",
        "cleaned_transcription": "Okay, are you catastrophising again?",
        "cleaned_translation":   "Okay, are you catastrophising again?"
      }
    ],
    "glossary_corrections": [
      { "heard": "cat distributing", "corrected": "catastrophising" }
    ]
  }
}
```

---

## File Responsibilities

| File | Role |
|---|---|
| `app.py` | Streamlit UI — file upload, progress bar, diff table, download |
| `pipeline.py` | Orchestration — group, batch, call LLM, align, assemble |
| `llm.py` | LLM factory — creates `ChatAnthropic` with API key and model config |
| `prompt.py` | All prompt text — system instructions and user message envelope |
| `schema.py` | Pydantic models — data shapes for input turns, cleaned output, and final document |

---

## Stage 1 — Segment Grouping

**Function:** `group_turns()` in `pipeline.py`

The ECS pipeline emits individual ASR segments (often 1–3 seconds each, sometimes a single phrase). Sending these individually to Claude gives it no conversational context. `group_turns()` merges consecutive same-speaker segments into a single **turn** before sending.

```
Input segments:
  Speaker 1  5.5s–9.8s   "haan I I I feel like mood bahut low hai"
  Speaker 1  9.8s–13.2s  "aur neend bhi nahi aati uh uh"

After group_turns():
  Turn 1  Speaker 1  5.5s–13.2s
    transcription: "haan I I I feel like mood bahut low hai aur neend bhi nahi aati uh uh"
    translation:   "yes I feel like mood is very low and sleep also does not come"
```

A speaker switch triggers a flush of the current turn and starts a new one.

---

## Stage 2 — Batching

**Function:** `batch_turns()` in `pipeline.py`

A 2-hour therapy session can produce 200+ turns. Sending all of them in one API call risks:
- Hitting the output token cap mid-JSON (unrecoverable)
- Extremely slow responses with no progress feedback

Each turn is serialised to JSON and its character length counted. Turns are packed into batches up to **80,000 characters** (≈ 20–30K tokens depending on script). Each batch is one independent Claude API call.

```
Why 80K chars and not 80K tokens?
  Counting exact tokens requires calling a tokenizer on every turn.
  Characters are a cheap proxy. The limit is sized conservatively so the
  cleaned-output JSON always fits inside the 16K max_tokens output cap.
  If truncation errors occur in production, reduce _BATCH_BUDGET, not
  max_tokens.
```

---

## Stage 3 — The Prompt

**Files:** `prompt.py`

The system prompt instructs Claude to act as a clinical transcript editor. It is structured with XML-style tags so Claude never confuses instructions with turn data. The user message wraps the batch in an `<input_batch>` envelope.

### Four cleanup passes (applied in order)

**Pass 1 — CLINICAL_TERMS**

Fix misheard psychiatric and medical vocabulary using the session context and optional user-supplied glossary. Common targets: CBT/DBT terminology, symptom vocabulary (anhedonia, dissociation), psychiatric medication names and dosages. Only correct when the intended term is unambiguous — if uncertain, leave as-is.

```
"cat distributing"  →  "catastrophising"
"ssri"              →  "SSRI"
"sir traline"       →  "sertraline"
```

**Pass 2 — MULTILINGUAL + SCRIPT_RESTORATION**

This is the most important pass for Indian clinical sessions. ASR systems routinely output Indian-language words in romanised Latin (phonetic spelling). The rule is:

- **Zero romanised Indian-language text** is allowed in `cleaned_transcription`
- Every romanised Indian-language word must be converted to its native script
- Only genuine English words and Latin-script proper nouns stay in Latin

```
"ab dekho"    →  "अब देखो"        (Hindi → Devanagari)
"theek hai"   →  "ठीक है"          (Hindi → Devanagari)
"naan solren" →  "நான் சொல்றேன்"   (Tamil → Tamil script)
"ami boli"    →  "আমি বলি"         (Bengali → Bengali script)

English loanwords inside Indian-language sentences stay Latin:
  "mood बहुत low है"  ✓  (mood, low are English loanwords)
  "मूड बहुत लो है"    ✗  (do not transliterate English into Devanagari)
```

The `cleaned_translation` is always fluent English — every Indian-language fragment is translated.

**Pass 3 — FORMATTING**

Reformat raw ASR output into clean, readable prose:

| Rule | Detail |
|---|---|
| Sentence boundaries | Capital first letter, correct terminal mark (`.` `?` `!`); danda (।) for pure Hindi sentences |
| Commas | Insert at natural pauses, do not over-comma |
| Capitalisation | CBT, DBT, ACT, SSRI, SNRI always uppercase; generic drug names lowercase (sertraline); no random emphasis caps |
| Interrupted speech | Em-dash (—) for mid-sentence breaks; ellipsis (…) for trailing meaningful pauses; no punctuation residue from filler removal |
| Paragraph breaks | `\n\n` only at genuine topic shifts within a long turn; never after every sentence |
| Numbers | Digits for clinical quantities ("50 mg", "3 weeks"); spelled-out for conversational context ("twice", "a few sessions") |
| Mixed-script spacing | Single space between Latin and Indic-script words: `"mood बहुत low है"` |
| Never in output | No markdown, no speaker labels, no `[inaudible]` brackets |

**Pass 4 — NOISE**

Remove ASR-captured artifacts with no meaning:
- Filler words: "uh", "um", "you know", "like" (when used as filler)
- Back-channels: "hmm", "mm-hmm", "haan haan"
- Stutters and false starts
- ASR doublings (same phrase repeated twice)

Preserve clinically meaningful signal even when it resembles noise:
- Emotionally weighted hesitation: "I just— I just can't" → keep
- Genuine tearful repetition → keep
- Patient self-corrections that change meaning → keep

### Glossary injection

The optional glossary the user types in the sidebar is injected as a `<glossary>` XML block inside the system prompt, treating it as data rather than additional instructions:

```xml
<glossary>
Apply these substitutions where they fit the surrounding context.
cat distributing → catastrophising
sukoon
plaud
</glossary>
```

---

## Stage 4 — Output Validation

Claude's output goes through five independent validation layers. Each layer is a fallback for the one above it. The system never crashes — the worst outcome is uncleaned text passed through unchanged.

### Layer 1 — API-level schema enforcement

`_invoke_cleaned_turns()` tries three paths in sequence:

```
Path 1: json_schema mode
  llm.with_structured_output(CleanedTurns, method="json_schema")
  → Anthropic API enforces the schema at generation time
  → Claude cannot produce a wrong type
  → Falls through to Path 2 if the API version doesn't support it

Path 2: tool_calling mode  (this is what runs almost always)
  llm.with_structured_output(CleanedTurns)
  → LangChain wraps CleanedTurns as a tool definition
  → Claude must "call" the tool with valid typed arguments
  → API validates tool arguments against the schema before returning
  → Falls through to Path 3 only if tool calling also fails

Path 3: raw invoke + manual parse
  llm.invoke(messages) → free-form text response
  → _is_truncated(): checks stop_reason == "max_tokens" first
      → if truncated: raises _OutputTruncatedError immediately
         (signals the caller to split — do not retry the same size)
  → _extract_message_text(): extracts string from response content blocks
  → _parse_json_object():
      1. Strip <think>...</think> reasoning blocks
      2. Try markdown fences (```json ... ```) — pick last valid one
      3. Walk char-by-char finding balanced { } objects — pick last one
  → CleanedTurns.model_validate(obj): Pydantic validates the dict
  → Raises ValueError if no valid JSON found
```

### Layer 2 — Output truncation: split and recurse

When the model hits `max_tokens`, the JSON is cut off mid-object. Retrying the same batch always fails the same way. `clean_batch()` catches `_OutputTruncatedError` and **splits the batch in half**, recursing each half independently:

```
Batch of 40 turns  →  hits max_tokens  →  split into 20 + 20
                                               │
                               ┌───────────────┴───────────────┐
                            20 turns                        20 turns
                            still truncates?                still truncates?
                            split to 10 + 10               split to 10 + 10
                                ...                             ...
                            eventually fits                 eventually fits
                               │                               │
                            CleanedTurns                   CleanedTurns
                               └───────────────┬───────────────┘
                                         merged result
```

Glossary corrections from both halves are merged and deduplicated. The recursion stops at a single turn — if a single turn still truncates (a pathologically long turn producing >16K output tokens), it is passed through uncleaned with a warning rather than looping forever.

### Layer 3 — Retry loop with correction message

For non-truncation failures (bad JSON, schema mismatch, empty response), `clean_batch()` retries up to **3 times**, appending a correction message to the conversation on each attempt so Claude sees what it got wrong:

```
Attempt 1:
  [SystemMessage, HumanMessage(batch)]

Attempt 2 (if attempt 1 failed):
  [SystemMessage, HumanMessage(batch),
   HumanMessage("Your previous reply did not match the output_contract.
                 turns must contain exactly N items with turn_index
                 values [0,1,2] in that order. No markdown.")]

Attempt 3 (if attempt 2 failed):
  [SystemMessage, HumanMessage(batch),
   HumanMessage(reminder 1),
   HumanMessage("Final attempt — emit valid JSON only.")]
```

Note: `_OutputTruncatedError` bypasses this loop entirely — there is no point retrying the same size batch.

### Layer 4 — Gap filling

`_align_to_batch()` runs after every successful `_invoke_cleaned_turns()` call. Even with a valid `CleanedTurns` object, Claude may have silently skipped a turn or returned a wrong `turn_index`. This function walks the original batch and fills any missing turns with the original uncleaned text:

```python
for b in batch:                         # walk original batch in order
    ct = by_idx.get(b.turn_index)
    if ct is None:                      # Claude skipped this turn
        # passthrough — original text, no data loss
        fixed.append(CleanedTurn(
            turn_index=b.turn_index,
            cleaned_transcription=b.transcription,
            cleaned_translation=b.translation,
        ))
    else:
        fixed.append(ct)                # use Claude's cleaned output
```

### Layer 5 — Total failure passthrough

If all three retry attempts in `clean_batch()` raise exceptions (after truncation splits have already been exhausted), `_fallback_identity()` is returned — every turn in the batch passes through with original text unchanged. The batch is logged as failed, but the rest of the document continues processing normally.

```
clean_batch() full decision tree:

  _OutputTruncatedError on any attempt
      → split batch in half, recurse each half independently
      → single-turn truncation → passthrough uncleaned (no infinite loop)

  other exception on attempt 1  →  log warning, retry
  other exception on attempt 2  →  log warning, retry
  other exception on attempt 3  →  _fallback_identity (original text, no crash)

  success on any attempt  →  _align_to_batch  →  done
```

**What is NOT validated:** content quality — whether Claude correctly restored the Devanagari, removed the right fillers, or fixed the right medication name. That is entirely the prompt's responsibility. The code validates only structure (correct keys, correct types, correct number of turns).

---

## Stage 5 — Assemble

**Function:** `assemble()` in `pipeline.py`

After all batches are processed, `assemble()` zips the cleaned turns back onto the original document. The original `segments` array is never modified. All cleaned data is written under a new `postprocess` key, so the output is always a strict superset of the input — nothing is lost.

Glossary corrections from all batches are deduplicated (by `heard + corrected` pair) and collected into a single `glossary_corrections` list in the final output.

---

## Glossary File

**Location:** `worker/glossary.json` (bundled in the Docker image at `/app/glossary.json`)

**Format:**

```json
{
  "corrections": [
    {"heard": "cat distributing", "corrected": "catastrophising"},
    {"heard": "sir traline",      "corrected": "sertraline"}
  ],
  "terms": [
    "sertraline",
    "escitalopram",
    "CBT",
    "DBT",
    "sukoon",
    "plaud"
  ]
}
```

**`corrections`** — explicit wrong → right mappings for ASR mishearings. These are applied by Claude when it sees the `heard` form in context.

**`terms`** — authoritative spellings that Claude must preserve exactly. No substitution rule — just tells Claude "this is the correct spelling, do not change it." Handles cases where ASR might phonetically mangle the same word differently each time (e.g. "setraline", "sertralene", "sir traline" → all should become `sertraline`).

**What gets sent to Claude** (via `load_glossary()`):

```
cat distributing → catastrophising
sir traline → sertraline
sertraline
escitalopram
CBT
DBT
sukoon
plaud
```

Injected into the system prompt inside a `<glossary>` XML block so Claude treats it as data, not additional instructions.

**Overriding at runtime:** set `GLOSSARY_FILE_PATH` to point to a custom file — useful for mounting a per-deployment glossary via ECS task definition environment variables or a Secrets Manager fetch.

---

## Output Schema

Defined in `schema.py` as Pydantic models:

```
PostprocessMeta
  model: str                        model ID used
  turns: list[PostprocessTurnOut]
  glossary_corrections: list[GlossaryCorrection]

PostprocessTurnOut
  turn_index: int
  speaker_id: int
  start_time: float
  end_time: float
  transcription: str                original ASR text
  translation: str                  original translation
  cleaned_transcription: str        Claude's cleaned transcription
  cleaned_translation: str          Claude's cleaned English translation

GlossaryCorrection
  heard: str                        what the ASR captured
  corrected: str                    what it should be
```

---

## Model Configuration

### Worker (automatic, in-pipeline)

| Env var | Default | Description |
|---|---|---|
| `POSTPROCESS_ENABLED` | `true` | Set `false` to skip entirely |
| `POSTPROCESS_MODEL` | `claude-sonnet-4-6` | Anthropic model ID |
| `ANTHROPIC_SECRET_NAME` | `anchor-voice/anthropic-api-key` | Secrets Manager secret (production) |
| `ANTHROPIC_API_KEY` | — | Direct key (local dev) |
| `GLOSSARY_FILE_PATH` | `/app/glossary.json` | Glossary file path inside the container |

### Standalone UI

| Setting | Default | Override |
|---|---|---|
| Model | `claude-opus-4-6` | UI dropdown |
| Available models | `claude-opus-4-6`, `claude-sonnet-4-6` | UI dropdown |
| Max output tokens | `16384` | `make_chat(max_tokens=...)` in `llm.py` |
| Temperature | `0.0` | `make_chat(temperature=...)` |
| Batch size | `80000` chars | `_BATCH_BUDGET` in `pipeline.py` |

**Choosing between models:**
- `claude-opus-4-6` — highest quality; best at script restoration and clinical term inference
- `claude-sonnet-4-6` — faster and cheaper; default in the worker for high-volume throughput

---

## Running Locally

### Worker (automatic)

```bash
cd worker

# add to .env
echo "ANTHROPIC_API_KEY=sk-ant-..." >> .env

# run pipeline — normalisation runs automatically if key is set
make run-local f=/path/to/audio.mp3

# disable normalisation for a run
POSTPROCESS_ENABLED=false make run-local f=/path/to/audio.mp3
```

The results JSON will contain `normalized_transcript` and `normalized_translation`
on every segment, plus a top-level `postprocess` key with the model ID and any
glossary corrections Claude made.

### Standalone UI

```bash
cd postprocess-ui

echo "ANTHROPIC_API_KEY=sk-ant-..." > .env
uv sync
uv run streamlit run app.py
```

The app runs at `http://localhost:8501`. Upload a `results.json` from the ECS pipeline, optionally type a custom glossary in the sidebar (one `wrong → right` or `term` per line), select a model, and click **Run post-processing**.
