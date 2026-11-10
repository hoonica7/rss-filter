"""Shared Gemini compatibility and run-local failure handling.

No API keys, prompts, private ranking profiles, or raw API errors are logged here.
"""

import math
import os
import re
import sys
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from google import genai
from google.genai import types


def create_client(api_key):
    # SDK 3 defaults to v1, which does not support our JSON response options.
    # Set the transport version explicitly for both classification and slides.
    api_version = (os.getenv("GEMINI_API_VERSION") or "").strip() or "v1beta"
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            api_version=api_version,
            # Rotation/cooldowns below own retries. Do not multiply calls in SDK.
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    )


def error_code(error):
    code = getattr(error, "code", None)
    try:
        return int(code)
    except (TypeError, ValueError):
        return None


def error_category(error):
    code = error_code(error)
    message = str(error).lower()
    if code == 429 or any(s in message for s in ("resource_exhausted", "quota", "rate limit")):
        return "quota"
    if code in (401, 403):
        return "auth"
    if code == 404:
        return "model"
    if code in (500, 502, 503, 504):
        return "unavailable"
    return "other"


def safe_error_summary(error):
    return f"{error_category(error)} (HTTP {error_code(error) or 'unknown'}, {type(error).__name__})"


def schema_is_unsupported(error):
    message = str(error).lower()
    return (
        error_code(error) in (None, 400)
        and "schema" in message
        and any(s in message for s in ("not supported", "unsupported", "not available"))
    )


def retry_after_seconds(error):
    """Honor HTTP Retry-After and Gemini RetryInfo; default to a minute."""
    delays = [60.0]
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", {}) or {}
    value = headers.get("Retry-After") or headers.get("retry-after")
    if value:
        try:
            delays.append(float(value))
        except (TypeError, ValueError):
            try:
                delays.append((parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
    # SDK error strings include the structured RetryInfo.retryDelay field.
    delays.extend(float(v) for v in re.findall(r"retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)s", str(error)))
    return max(delay for delay in delays if math.isfinite(delay))


class FailureTracker:
    """Avoid hammering failed key/model pairs on every subsequent batch."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.blocked_until = {}

    def can_try(self, key_label, model):
        return self.clock() >= self.blocked_until.get((key_label, model), 0)

    def record(self, key_label, model, error):
        category = error_category(error)
        if category in ("auth", "model"):
            until = math.inf  # Configuration/availability will not heal this run.
        elif category == "quota":
            message = str(error).lower()
            daily = any(s in message for s in ("perday", "per_day", "per day", "daily"))
            until = math.inf if daily else self.clock() + retry_after_seconds(error)
        elif category == "unavailable":
            until = self.clock() + retry_after_seconds(error)
        else:
            return category
        self.blocked_until[(key_label, model)] = until
        return category


def classification_status(classified, pending):
    return f"Gemini classification: {classified} classified, {pending} pending retry."


def check_classification_health(classified, pending):
    """Call only after saving outputs/checkpoints, so failure cannot lose work."""
    message = classification_status(classified, pending)
    summary_path = os.getenv("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(message + "\n")
    if pending:
        level = "error" if classified == 0 else "warning"
        print(f"::{level}::{message}", file=sys.stderr)
    if pending and classified == 0:
        raise RuntimeError(message + " No Gemini decisions completed; pending queue preserved.")
