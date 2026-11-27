# Phase 0: Accounts and tools

## What a contract like this needs on day one

On a real forward-deployed contract, the first week goes on **access**: a cloud account, model
access, an observability tool and a repo with CI. Nothing gets built until those exist. The
same goes for this project.

| Thing | Why it exists | What we use |
|---|---|---|
| Cloud account | Somewhere to run code that isn't your laptop | AWS, Free plan |
| Identity (IAM) | Who is allowed to do what in that account | an IAM admin user for you, an OIDC role for GitHub |
| Model access | The LLM the agent calls | Amazon Bedrock, Nova Lite |
| Observability | Seeing what the agent did on each step, how long it took and what it cost | Langfuse Cloud |
| Source and CI | Where the code lives, and the robot that tests every change | GitHub + Actions |
| Infrastructure as code | Cloud resources written as files, so they can be reviewed, repeated and deleted | Terraform |

## Tools installed on your PC

- **uv**: the Python package and project manager. It created `pyproject.toml` (the list of
  dependencies) and `uv.lock` (the exact versions, so CI installs the same thing your laptop has).
- **AWS CLI**: talks to AWS from the terminal. `aws login` borrows your browser's console
  session, so no long-lived access keys are saved on disk.
- **Terraform**: reads `.tf` files and makes AWS match them.
- **gh**: the GitHub CLI, already logged in as waLLxAck.

## The AWS Free plan

The Free plan gives a new account up to $200 of credits for 6 months. It **cannot bill you**:
when the credits or the 6 months run out, AWS asks you to upgrade, and if you don't, it closes
the account. Some services that could burn credits quickly are blocked, such as Marketplace,
which is why Claude models on Bedrock aren't available here and Amazon Nova is.

Some services are free on any plan, within monthly limits:
- Lambda: 1M requests and 400,000 GB-seconds a month
- SQS: 1M requests a month
- DynamoDB: 25 GB of storage and 25 read/write capacity units

### Root user and IAM user

The email you sign up with becomes the **root user**. It can do anything, including closing the
account, so you put MFA on it and only use it for billing. Day-to-day work goes through an **IAM
user**. Least privilege is the habit clients look for: each identity gets only the permissions it
needs. Later, GitHub Actions gets its own narrow role, and it signs in through **OIDC**: GitHub
proves "I am this repo's workflow" and AWS hands back temporary credentials. No secret is stored.

## Langfuse

Langfuse records a **trace** for every agent run. A trace is a tree of steps (each LLM call,
tool call and guardrail check), each with its inputs, outputs, latency, tokens and cost. It
also stores **datasets** (our labelled test tickets) and **scores** (eval results), so you can
compare one prompt or model version against the next. Hobby is free: 50k units a month, where
traces, observations and scores each count as units.
