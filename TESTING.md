# Gemini regression checks

Create a virtual environment, install `requirements-dev.txt`, then run:

```sh
python -m pytest -q
python -m compileall -q Filter_RSS.py enhance_slides.py run_filter_resilient.py gemini_runtime.py restore_checkpoint.py
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


## Cache-loss and historical-backlog recovery

`restore_checkpoint.py` reconciles the restored cache with pending data and
import markers from the current `gh-pages` commit. Both pending queues are
authoritative: an older audit cannot erase an explicit retry from either one.
Without reliable provenance this may reclassify an article, but cannot silently
lose unresolved work. If no cache entry was restored, it restores only known
checkpoint/output data from that commit, never source code. Missing or invalid pending data stops
the run instead of silently treating the backlog as empty. Production runs are
serialized to avoid two workflows overwriting each other's checkpoint. Cache
keys include the run attempt so reruns do not restore their own old exact key.

After reviewing and merging this change, a production manual dispatch can take
`historical_pending_ref`, a full 40-character commit SHA containing a historical
`pending_classification_queue.json`. The script merges that queue with current
pending work and excludes only completed audit identities in the same journal.
Imported items before a resumed journal remain pending through every partial
checkpoint and are processed on the next full pass. Title-only matches are
deliberately retained: preprints and published versions
may use different source thresholds. Existing article metadata is preserved.

For the November 10 cache miss, the verified recovery source is
`403c0077281423efa847d40ba388f2208326b646`. Against published audit
`dd2ca9610b9699f1c144eadd79177712d80ce635`, its 744 historical records reconcile to
306 already decided identities plus 438 records to recover. These numbers may
change with newer completed audits. A recovery marker is saved alongside the
pending queue in the cache, artifacts, and published output so importing the
same source again does not requeue completed papers. `backlog_recovery_report.json`
is included in the output artifact for verification.

Dispatching this production workflow invokes Gemini, publishes Pages, and may
send result or failure emails. Do not dispatch just to test recovery. Offline
unit tests cover cache misses, prefix cache hits, malformed state, duplicate
imports, interrupted writes, and preserving current pending articles. A local
Git fixture can validate the recovery CLI without external calls. Recovery is
not complete until the actual run's audit accounts for every restored identity
as decided or still pending; an empty queue alone is not sufficient evidence.

Limit: the `gh-pages` fallback is the last successful publication, not an
always-written durable checkpoint for later failed runs. If a newer failed
run's cache is lost, recover its retained `filtered-files` artifact before
proceeding. This change does not claim to recover state that no longer exists.
