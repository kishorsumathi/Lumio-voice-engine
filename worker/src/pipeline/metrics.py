"""
CloudWatch metrics via Embedded Metric Format (EMF).

We emit a specially-shaped JSON log line to stdout. CloudWatch Logs
auto-extracts the values as metrics under the `AnchorVoice` namespace, so
there's zero PutMetricData API cost, zero additional IAM permissions, and
zero new infra. All we need is the awslogs driver already wired up for the
ECS task — the same stream that carries our regular logs.

Docs: https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch_Embedded_Metric_Format_Specification.html

Usage:

    emit(
        metrics={"JobCompleted": (1, "Count"), "JobDurationSeconds": (187.3, "Seconds")},
        dimensions={"Service": "worker"},
    )

Notes:
- Every dimension key is also included as a top-level field in the JSON
  (required by EMF).
- `Timestamp` is epoch milliseconds (EMF requirement).
- The log line is `print()`'d directly so it goes to stdout regardless of
  structlog/loguru config — CloudWatch cares about the exact JSON shape,
  not the log framework.
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Iterable

_NAMESPACE = os.getenv("METRICS_NAMESPACE", "AnchorVoice")
_ENABLED = os.getenv("METRICS_ENABLED", "1") not in ("0", "false", "False", "")


def emit(
    metrics: dict[str, tuple[float, str]],
    dimensions: dict[str, str] | None = None,
    namespace: str | None = None,
) -> None:
    """
    Emit one EMF log line containing one or more metrics under a single
    dimension set.

    Parameters
    ----------
    metrics : {name: (value, unit)}
        Unit must be a valid CloudWatch unit, e.g. "Count", "Seconds",
        "Milliseconds", "Percent", "None".
    dimensions : {name: value}
        CloudWatch dimensions for this datum. All values become slicing
        keys in the console. Keep cardinality bounded (status, language —
        NOT job_id).
    namespace : str, optional
        Override the default namespace (for testing).
    """
    if not _ENABLED or not metrics:
        return

    dims = dimensions or {}
    ns = namespace or _NAMESPACE

    payload: dict = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": ns,
                "Dimensions": [list(dims.keys())] if dims else [[]],
                "Metrics": [
                    {"Name": name, "Unit": unit}
                    for name, (_value, unit) in metrics.items()
                ],
            }],
        }
    }
    payload.update(dims)
    for name, (value, _unit) in metrics.items():
        payload[name] = value

    print(json.dumps(payload, separators=(",", ":")), file=sys.stdout, flush=True)


def emit_job_outcome(
    status: str,
    wall_clock_s: float,
    audio_duration_s: float,
    num_segments: int,
    num_speakers: int,
    num_chunks: int,
) -> None:
    """Emit one datum per terminal job outcome (completed | failed)."""
    count_name = "JobCompleted" if status == "completed" else "JobFailed"
    emit(
        metrics={
            count_name: (1, "Count"),
            "JobDurationSeconds": (wall_clock_s, "Seconds"),
            "AudioDurationSeconds": (audio_duration_s, "Seconds"),
            "SegmentsProcessed": (num_segments, "Count"),
            "SpeakersDetected": (num_speakers, "Count"),
            "ChunksProcessed": (num_chunks, "Count"),
        },
        dimensions={"Service": "worker"},
    )


def emit_translation_coverage(
    language: str,
    empty_segments: int,
    nonempty_source: int,
) -> None:
    """Emit translation coverage per target language."""
    fail_rate_pct = (100.0 * empty_segments / nonempty_source) if nonempty_source else 0.0
    emit(
        metrics={
            "TranslationSegments": (nonempty_source, "Count"),
            "TranslationEmptySegments": (empty_segments, "Count"),
            "TranslationEmptyRate": (fail_rate_pct, "Percent"),
        },
        dimensions={"Service": "worker", "Language": language},
    )


def emit_counter(name: str, value: float = 1, **dimensions: str) -> None:
    """Small helper for ad-hoc counters."""
    emit(
        metrics={name: (value, "Count")},
        dimensions={"Service": "worker", **dimensions},
    )


def iter_metric_names() -> Iterable[str]:
    """Used by tests / dashboard scripts to know which metrics we emit."""
    return (
        "JobCompleted",
        "JobFailed",
        "JobDurationSeconds",
        "AudioDurationSeconds",
        "SegmentsProcessed",
        "SpeakersDetected",
        "ChunksProcessed",
        "TranslationSegments",
        "TranslationEmptySegments",
        "TranslationEmptyRate",
    )
