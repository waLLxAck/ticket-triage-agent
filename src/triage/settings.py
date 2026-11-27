"""Load secrets in Lambda from SSM Parameter Store, once per cold start.

The Lambda environment only holds parameter *names*. The values (Langfuse keys, the API key)
live in SSM as SecureString, encrypted with KMS, and are fetched at start-up. That keeps them out
of the Lambda console, the Terraform plan output and CloudWatch logs. Standard-tier SSM
parameters are free.

Locally, `.env` provides the same variables, so this does nothing.
"""

import os
from functools import cache

import boto3

# env var that should hold the value -> env var that holds the SSM parameter name
_FROM_SSM = {
    "LANGFUSE_PUBLIC_KEY": "LANGFUSE_PUBLIC_KEY_PARAM",
    "LANGFUSE_SECRET_KEY": "LANGFUSE_SECRET_KEY_PARAM",
    "API_KEY": "API_KEY_PARAM",
}


@cache
def load() -> None:
    wanted = {var: os.environ[p] for var, p in _FROM_SSM.items() if os.environ.get(p)}
    if not wanted:
        return
    resp = boto3.client("ssm").get_parameters(Names=list(wanted.values()), WithDecryption=True)
    by_name = {p["Name"]: p["Value"] for p in resp["Parameters"]}
    for var, name in wanted.items():
        if name in by_name:
            os.environ[var] = by_name[name]
