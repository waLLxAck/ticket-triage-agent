# The application stack: everything the running service needs.
#
#   customer ──HTTPS──► Lambda Function URL ─► triage-api (FastAPI)
#                                               │ redact, store (DynamoDB), enqueue (SQS)
#                                               ▼
#                       SQS triage-tickets ──► triage-worker (LangGraph agent ─► Bedrock Nova)
#                         │ 3 failed attempts      │ result ─► DynamoDB · traces ─► Langfuse
#                         ▼
#                       SQS triage-tickets-dlq ──► CloudWatch alarm ─► email
#
# Deployed by GitHub Actions from main (infra/bootstrap gives it the role), or locally:
#   python scripts/build_lambda.py && terraform -chdir=infra/app apply

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 6.0" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
  # Remote state in the bootstrap bucket. `use_lockfile` makes Terraform write a .tflock object
  # next to the state, so two applies (your laptop and CI) can't run at the same time.
  backend "s3" {
    key          = "app/terraform.tfstate"
    region       = "eu-north-1"
    use_lockfile = true
    encrypt      = true
    # bucket is passed at init time: -backend-config="bucket=triage-tfstate-<account id>"
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { project = "ticket-triage-agent", managed_by = "terraform", layer = "app" }
  }
}

data "aws_caller_identity" "me" {}

locals {
  name      = "triage"
  model_arn = "arn:aws:bedrock:${var.region}::foundation-model/${var.model_id}"
  # Worker concurrency × per-worker rate must stay under the account's Bedrock quota (20/min).
  worker_concurrency = 2
  per_worker_rpm     = 9
  max_receives       = 3 # attempts before a message moves to the DLQ
  app_boundary_arn   = "arn:aws:iam::${data.aws_caller_identity.me.account_id}:policy/triage-app-boundary"
}

# --- Secrets: SSM Parameter Store (SecureString, free standard tier) ------------------------------

resource "random_password" "api_key" {
  length  = 40
  special = false
}

resource "aws_ssm_parameter" "api_key" {
  name  = "/triage/api_key"
  type  = "SecureString"
  value = random_password.api_key.result
}

resource "aws_ssm_parameter" "langfuse_public_key" {
  name  = "/triage/langfuse_public_key"
  type  = "SecureString"
  value = var.langfuse_public_key
}

resource "aws_ssm_parameter" "langfuse_secret_key" {
  name  = "/triage/langfuse_secret_key"
  type  = "SecureString"
  value = var.langfuse_secret_key
}

# --- State: DynamoDB --------------------------------------------------------------------------------

resource "aws_dynamodb_table" "tickets" {
  name     = "${local.name}-tickets"
  hash_key = "ticket_id"
  # Provisioned 5/5 stays inside the always-free 25 RCU / 25 WCU. On-demand would bill per
  # request (tiny, but not free).
  billing_mode   = "PROVISIONED"
  read_capacity  = 5
  write_capacity = 5

  attribute {
    name = "ticket_id"
    type = "S"
  }

  server_side_encryption { enabled = true }
}

# --- Queue: SQS with a dead-letter queue --------------------------------------------------------------

resource "aws_sqs_queue" "dlq" {
  name                      = "${local.name}-tickets-dlq"
  message_retention_seconds = 1209600 # 14 days to inspect failures
}

resource "aws_sqs_queue" "tickets" {
  name = "${local.name}-tickets"
  # Must exceed the worker timeout, or a message reappears while it's still being processed.
  # AWS recommends 6x the function timeout.
  visibility_timeout_seconds = 6 * var.worker_timeout_s
  receive_wait_time_seconds  = 20 # long polling: fewer empty receives, fewer requests
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.dlq.arn
    maxReceiveCount     = local.max_receives
  })
}

# --- Logs ------------------------------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "api" {
  name              = "/aws/lambda/${local.name}-api"
  retention_in_days = 7
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/aws/lambda/${local.name}-worker"
  retention_in_days = 7
}

# --- Code artefact ---------------------------------------------------------------------------------------

resource "aws_s3_object" "lambda_zip" {
  bucket = var.artifacts_bucket
  key    = "lambda/${filemd5(var.lambda_zip)}.zip" # new key per build, so Lambda sees a change
  source = var.lambda_zip
}
