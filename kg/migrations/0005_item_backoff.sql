-- Retry backoff for pipeline items (D.1: "a failure is retried with backoff"; lesson L7).
--
-- next_attempt_at: the earliest time a queued item may be started again, set by
-- State.item_failed from the runner's backoff schedule (pipeline.run.retry_backoff_min).
-- NULL = due now. State.eligible leaves out items not yet due; State.backlog still counts
-- them, so a backed-off item is never hidden from the backlog. requeue clears it.

ALTER TABLE item ADD COLUMN next_attempt_at TEXT;
