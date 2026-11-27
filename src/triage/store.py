"""DynamoDB persistence: one item per ticket, keyed by ticket_id.

Ticket lifecycle (the `status` attribute):

    queued ──worker──► auto_resolved            (reply sent automatically)
                  └──► awaiting_review ──human──► human_approved | human_edited
                  └──► failed                   (after 3 attempts the message goes to the DLQ)

Only the *redacted* ticket text is ever stored: the API redacts before it writes. That's data
minimisation again, this time for data at rest.
"""

import os
import time
from decimal import Decimal
from functools import cache

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError


class AlreadyProcessed(Exception):
    """The ticket already left the state this write expects (e.g. a redelivered SQS message)."""


@cache
def _table():
    return boto3.resource("dynamodb").Table(os.environ["TICKETS_TABLE"])


def _to_dynamo(value):
    """DynamoDB has no float type: numbers must be Decimal."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_dynamo(v) for v in value]
    return value


def _from_dynamo(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _from_dynamo(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_from_dynamo(v) for v in value]
    return value


def create(ticket_id: str, redacted_text: str, pii: list[str]) -> dict:
    item = {
        "ticket_id": ticket_id,
        "status": "queued",
        "text": redacted_text,
        "pii_redacted": pii,
        "created_at": int(time.time()),
    }
    _table().put_item(Item=item, ConditionExpression="attribute_not_exists(ticket_id)")
    return item


def _conditional(**kwargs):
    try:
        return _table().update_item(**kwargs)
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise AlreadyProcessed(kwargs["Key"]["ticket_id"]) from None
        raise


def save_result(ticket_id: str, result: dict) -> str:
    """Store the agent's result. Only a `queued` ticket can be triaged.

    SQS delivers *at least once*: the same message can arrive twice (a retry after a timeout, a
    partial batch failure). Without the condition, a redelivery could overwrite a human's
    approval and put the ticket back in the review queue. The condition makes the write
    idempotent: the second attempt raises AlreadyProcessed and the worker drops it.
    """
    status = "auto_resolved" if result["action"] == "auto_resolve" else "awaiting_review"
    _conditional(
        Key={"ticket_id": ticket_id},
        ConditionExpression=Attr("status").eq("queued"),
        UpdateExpression="SET #s = :s, #r = :r, triaged_at = :t",
        ExpressionAttributeNames={"#s": "status", "#r": "result"},
        ExpressionAttributeValues={
            ":s": status,
            ":r": _to_dynamo(result),
            ":t": int(time.time()),
        },
    )
    return status


def mark_failed(ticket_id: str, error: str) -> None:
    _conditional(
        Key={"ticket_id": ticket_id},
        ConditionExpression=Attr("status").eq("queued"),  # never overwrite a finished ticket
        UpdateExpression="SET #s = :s, #e = :e",
        ExpressionAttributeNames={"#s": "status", "#e": "error"},
        ExpressionAttributeValues={":s": "failed", ":e": error[:500]},
    )


def get(ticket_id: str) -> dict | None:
    item = _table().get_item(Key={"ticket_id": ticket_id}).get("Item")
    return _from_dynamo(item) if item else None


def awaiting_review(limit: int = 50) -> list[dict]:
    """Escalated tickets, newest first.

    A Scan reads the whole table, and DynamoDB applies the filter *after* reading each page (at
    most 1 MB), so we must follow LastEvaluatedKey or tickets beyond the first page would never
    show up. Fine for a demo. At volume, add a GSI on (status, created_at) and Query it.
    """
    items, kwargs = [], {"FilterExpression": Attr("status").eq("awaiting_review")}
    while True:
        resp = _table().scan(**kwargs)
        items.extend(resp["Items"])
        if "LastEvaluatedKey" not in resp:
            break
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    items.sort(key=lambda i: i["created_at"], reverse=True)
    return [_from_dynamo(i) for i in items[:limit]]


def review(ticket_id: str, reviewer: str, reply: str, edited: bool) -> dict:
    resp = _conditional(
        Key={"ticket_id": ticket_id},
        UpdateExpression="SET #s = :s, final_reply = :r, reviewed_by = :by, reviewed_at = :t",
        ConditionExpression=Attr("status").eq("awaiting_review"),
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":s": "human_edited" if edited else "human_approved",
            ":r": reply,
            ":by": reviewer,
            ":t": int(time.time()),
        },
        ReturnValues="ALL_NEW",
    )
    return _from_dynamo(resp["Attributes"])
