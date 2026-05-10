# Transcript post-processing (Streamlit)

Upload an Anchor Voice pipeline **results JSON** (same schema as the worker writes to S3: `segments[]` with `transcription`, `translation`, `speaker_id`, timestamps). This app calls **Amazon Bedrock (Claude Sonnet 4.6)** via LangChain and produces:

- `cleaned_transcription` / `cleaned_translation` per merged speaker turn
- A merged `postprocess` object on the downloaded JSON

## Setup

```bash
cd postprocess-ui
uv sync
```

Create `postprocess-ui/.env` with your Bedrock API key and region:

```bash
AWS_BEARER_TOKEN_BEDROCK=...
AWS_REGION=us-west-2
# Optional — overrides the sidebar default when set before first run:
# BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-6
```

```bash
uv run streamlit run app.py
```

Open the URL Streamlit prints (usually http://localhost:8501).

## Notes

- Pick an inference **model ID** in the sidebar that your account can invoke (`us.…`, `global.…`, or in-region `anthropic.…`), matching [Bedrock inference profiles](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-support.html).
- Long sessions are split into batches automatically based on the selected model's char budget.
- Requires network access to reach Amazon Bedrock.
