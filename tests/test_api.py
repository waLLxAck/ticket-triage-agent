"""API and storage tests against moto, an in-memory fake of DynamoDB and SQS.

They prove the wiring (auth, redaction before storage, enqueueing, the review state machine)
without an AWS account, in under a second.
"""

import json

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

KEY = "test-key"


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-north-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("API_KEY", KEY)
    monkeypatch.setenv("TICKETS_TABLE", "triage-tickets")
    with mock_aws():
        boto3.client("dynamodb").create_table(
            TableName="triage-tickets",
            KeySchema=[{"AttributeName": "ticket_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "ticket_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        queue_url = boto3.client("sqs").create_queue(QueueName="triage-tickets")["QueueUrl"]
        monkeypatch.setenv("QUEUE_URL", queue_url)
        from triage import api, store

        store._table.cache_clear()
        api._sqs.cache_clear()
        yield TestClient(api.app), queue_url


def test_requests_without_the_key_are_rejected(env):
    client, _ = env
    assert client.post("/tickets", json={"text": "hi"}).status_code == 401
    assert (
        client.post("/tickets", json={"text": "hi"}, headers={"x-api-key": "nope"}).status_code
        == 401
    )


def test_submit_redacts_stores_and_enqueues(env):
    client, queue_url = env
    resp = client.post(
        "/tickets",
        json={"text": "Card 4539 1488 0343 6467 charged twice"},
        headers={"x-api-key": KEY},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["pii_redacted"] == ["CARD"]

    stored = client.get(f"/tickets/{body['ticket_id']}", headers={"x-api-key": KEY}).json()
    assert stored["status"] == "queued"
    assert "4539" not in stored["text"]

    msgs = boto3.client("sqs").receive_message(QueueUrl=queue_url)["Messages"]
    assert "4539" not in msgs[0]["Body"]
    assert json.loads(msgs[0]["Body"])["ticket_id"] == body["ticket_id"]


def test_review_flow(env):
    from triage import store

    client, _ = env
    store.create("t1", "fraud ticket", [])
    store.save_result("t1", {"action": "escalate", "reply": "holding", "confidence": 0.9})

    queue = client.get("/review", headers={"x-api-key": KEY}).json()
    assert [t["ticket_id"] for t in queue] == ["t1"]

    done = client.post(
        "/tickets/t1/review",
        json={"reply": "Our fraud team has frozen your card.", "edited": True},
        headers={"x-api-key": KEY},
    )
    assert done.status_code == 200 and done.json()["status"] == "human_edited"

    # Reviewing twice is refused: the item is no longer awaiting review.
    again = client.post("/tickets/t1/review", json={"reply": "x"}, headers={"x-api-key": KEY})
    assert again.status_code == 409
    assert client.get("/review", headers={"x-api-key": KEY}).json() == []


def test_auto_resolved_tickets_never_enter_the_review_queue(env):
    from triage import store

    client, _ = env
    store.create("t2", "double charge", [])
    assert store.save_result("t2", {"action": "auto_resolve", "reply": "ok"}) == "auto_resolved"
    assert client.get("/review", headers={"x-api-key": KEY}).json() == []
