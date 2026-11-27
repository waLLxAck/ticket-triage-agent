"""API Lambda: FastAPI behind a Lambda Function URL, adapted by Mangum.

Endpoints (all JSON endpoints need the `x-api-key` header):
    POST /tickets                 submit a ticket          → 202 {ticket_id, status: queued}
    GET  /tickets/{id}            ticket state and result
    GET  /review                  escalated tickets waiting for a human
    POST /tickets/{id}/review     a human approves or edits the reply
    GET  /                        the human review page (HTML; asks for the API key)

How the pieces connect:
- **Lambda Function URL**: a free HTTPS endpoint built into Lambda (no API Gateway needed). AWS
  turns each HTTP request into a Lambda event.
- **Mangum** translates that event into an ASGI request for FastAPI, and the response back.
- The API does *no* LLM work. It redacts, stores and enqueues, so it answers in milliseconds.
"""

import hmac
import json
import os
import uuid
from functools import cache
from pathlib import Path

import boto3
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from mangum import Mangum
from pydantic import BaseModel, Field

from triage import settings, store
from triage.guardrails import redact

settings.load()
app = FastAPI(title="Acme Pay ticket triage (demo)")


@cache
def _sqs():
    return boto3.client("sqs")


def require_key(x_api_key: str = Header(default="")) -> None:
    expected = os.environ.get("API_KEY", "")
    # compare_digest takes the same time whether the first or last character is wrong, so an
    # attacker can't guess the key one character at a time by timing responses.
    if not expected or not hmac.compare_digest(x_api_key, expected):
        raise HTTPException(status_code=401, detail="missing or wrong x-api-key")


class NewTicket(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class Review(BaseModel):
    reply: str = Field(min_length=1, max_length=4000)
    reviewer: str = Field(default="support-agent", max_length=80)
    edited: bool = False


@app.post("/tickets", status_code=202, dependencies=[Depends(require_key)])
def submit(ticket: NewTicket):
    ticket_id = uuid.uuid4().hex[:12]
    clean, pii = redact(ticket.text)  # raw text is never stored or queued
    store.create(ticket_id, clean, pii)
    try:
        _sqs().send_message(
            QueueUrl=os.environ["QUEUE_URL"],
            MessageBody=json.dumps({"ticket_id": ticket_id, "text": clean}),
        )
    except Exception as exc:  # noqa: BLE001 - don't leave a 'queued' ticket nothing will process
        store.mark_failed(ticket_id, f"enqueue failed: {exc!r}")
        raise HTTPException(status_code=503, detail="could not queue the ticket, retry") from None
    return {"ticket_id": ticket_id, "status": "queued", "pii_redacted": pii}


@app.get("/tickets/{ticket_id}", dependencies=[Depends(require_key)])
def get_ticket(ticket_id: str):
    item = store.get(ticket_id)
    if not item:
        raise HTTPException(status_code=404, detail="no such ticket")
    return item


@app.get("/review", dependencies=[Depends(require_key)])
def review_queue():
    return store.awaiting_review()


@app.post("/tickets/{ticket_id}/review", dependencies=[Depends(require_key)])
def review(ticket_id: str, body: Review):
    try:
        return store.review(ticket_id, body.reviewer, body.reply, body.edited)
    except store.AlreadyProcessed:
        raise HTTPException(status_code=409, detail="ticket is not awaiting review") from None


@app.get("/", response_class=HTMLResponse)
def review_page():
    return (Path(__file__).parent / "review.html").read_text(encoding="utf-8")


handler = Mangum(app, lifespan="off")
