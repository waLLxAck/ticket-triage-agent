"""Run one ticket from the terminal:  uv run triage "I was charged twice for my coffee" """

import sys

from dotenv import load_dotenv

from triage.graph import triage
from triage.llm import BedrockLLM


def main() -> None:
    load_dotenv()  # reads .env: region, model id, Langfuse keys
    sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252
    text = " ".join(sys.argv[1:]) or sys.stdin.read()
    result = triage(text, BedrockLLM())
    print(result.model_dump_json(indent=2))


if __name__ == "__main__":
    main()
