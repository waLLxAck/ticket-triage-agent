"""Run one ticket from the terminal:  uv run triage "I was charged twice for my coffee" """

import sys
import uuid

from dotenv import load_dotenv

from triage import tracing
from triage.graph import triage
from triage.llm import BedrockLLM


def main() -> None:
    load_dotenv()  # reads .env: region, model id, Langfuse keys
    sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252
    text = " ".join(sys.argv[1:]) or sys.stdin.read()
    ticket_id = uuid.uuid4().hex[:8]
    result = triage(text, BedrockLLM(), ticket_id, config=tracing.run_config(ticket_id, ["cli"]))
    print(result.model_dump_json(indent=2))
    if tracing.enabled():
        tracing.client().flush()  # traces are sent in the background, so flush before exiting


if __name__ == "__main__":
    main()
