# Gemini regression checks

Create a virtual environment, install `requirements-dev.txt`, then run:

```sh
python -m pytest -q
python -m compileall -q Filter_RSS.py enhance_slides.py run_filter_resilient.py gemini_runtime.py
```

Tests use synthetic article data and responses, clear API-key/private-profile
variables in the test process, and forbid socket connections. The real pinned
SDK is used to verify request serialization and the `/v1beta/` endpoint. These
checks do **not** prove that a production API key has quota or access to a model.

Both scripts share an explicit `GEMINI_API_VERSION` (default `v1beta`). The
workflow pins this value and installs `google-genai==3.0.0` without upgrading it
again afterward. Keep the SDK pin and endpoint regression test together when
upgrading.

429s honor `Retry-After` / `RetryInfo.retryDelay`, with a minimum one-minute
cooldown per key/model. Daily quota, authentication, and model-not-found errors
disable that pair for the rest of the process. Other pairs can still run; batches
with no usable pair remain pending for the next scheduled run. There is no sleep
loop or immediate retry of exhausted pairs. SDK automatic retries are disabled
so they do not multiply application-level rotation.

After output/checkpoint writes, a run with pending papers but no Gemini decisions
raises an error, preventing the normal success deployment/email steps. The
existing always-run artifact/cache steps preserve the pending queue. Partial
classification produces a warning and a count summary in the audit email and
Actions summary, while retaining the existing partial-output behavior. The
`SUCCESS` checkpoint means the journal loop finished, not that all classifications
completed; it lets the next run revisit all journals and the saved queue.

Before production validation, review the branch/diff and apply it through the
normal repository process. Running the production workflow calls external feeds
and Gemini, writes Pages output, and can send mail. The regression tests do none
of these actions.

The `Gemini regression and safe smoke test` workflow runs the offline suite on
pull requests. Its manual `workflow_dispatch` run additionally classifies exactly
one synthetic paper with the configured API keys/models. It supplies no private
ranking profile, never fetches feeds, and cannot deploy Pages or send email.
Success verifies a real API decision, not production backlog completion.
