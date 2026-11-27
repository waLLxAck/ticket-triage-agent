"""Worker Lambda: triggered by SQS, runs one ticket through the agent, stores the result.

Why a queue between the API and the agent?
- **Buffering:** a burst of 500 tickets doesn't become 500 parallel Bedrock calls against a
  quota of 20 a minute. SQS holds them, and the event source mapping's `maximum_concurrency`
  caps how many workers run at once.
- **Retries:** if a run fails (throttling, a timeout), the message becomes visible again after
  the visibility timeout and is retried. After 3 failures it goes to a **dead-letter queue**
  for a human to inspect, so it's never silently lost.
- **Fast API:** the customer-facing endpoint returns 202 in milliseconds instead of waiting
  around 5 seconds for three LLM calls.

`ReportBatchItemFailures`: we return the IDs of messages that failed, so SQS retries only
those and not the whole batch.
"""

import json
import logging
import os

from triage import settings, store, tracing
from triage.graph import triage
from triage.llm import BedrockLLM

log = logging.getLogger()
log.setLevel(logging.INFO)


def handler(event, context):
    settings.load()
    failures = []
    max_receives = int(os.environ.get("MAX_RECEIVES", "3"))  # = the queue's maxReceiveCount
    for record in event["Records"]:
        ticket_id = None
        try:
            msg = json.loads(record["body"])  # inside the try: one bad message can't sink the batch
            ticket_id = msg["ticket_id"]
            cfg = tracing.run_config(ticket_id, ["lambda"])
            result = triage(msg["text"], BedrockLLM(), ticket_id, config=cfg)
            status = store.save_result(ticket_id, result.model_dump(mode="json"))
            # Structured log line: CloudWatch Logs Insights can query these fields.
            log.info(
                json.dumps(
                    {
                        "ticket_id": ticket_id,
                        "status": status,
                        "category": result.category,
                        "cost_usd": result.cost_usd,
                        "latency_ms": result.latency_ms,
                    }
                )
            )
        except store.AlreadyProcessed:
            log.info(json.dumps({"ticket_id": ticket_id, "status": "duplicate delivery, skipped"}))
        except Exception as exc:
            log.exception("ticket %s failed", ticket_id)
            receive_count = int(record["attributes"].get("ApproximateReceiveCount", "1"))
            failures.append({"itemIdentifier": record["messageId"]})
            if ticket_id and receive_count >= max_receives:  # last attempt, the DLQ is next
                try:
                    store.mark_failed(ticket_id, repr(exc))
                except Exception:
                    log.exception("could not mark %s failed", ticket_id)
    if tracing.enabled():
        tracing.client().flush()  # Lambda freezes after return: send traces now
    return {"batchItemFailures": failures}
