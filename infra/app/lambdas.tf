# --- IAM: one role per function, each with only what it needs (least privilege) ---------------

data "aws_iam_policy_document" "lambda_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "api" {
  name                 = "triage-app-api"
  assume_role_policy   = data.aws_iam_policy_document.lambda_trust.json
  permissions_boundary = local.app_boundary_arn # required: the deploy role can't create one without it
}

resource "aws_iam_role_policy" "api" {
  role = aws_iam_role.api.id
  name = "api"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.api.arn}:*" },
      { Effect = "Allow", Action = ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:Scan"], Resource = aws_dynamodb_table.tickets.arn },
      { Effect = "Allow", Action = ["sqs:SendMessage"], Resource = aws_sqs_queue.tickets.arn },
      { Effect = "Allow", Action = ["ssm:GetParameters"], Resource = aws_ssm_parameter.api_key.arn },
      # Note what's missing: the API can't call Bedrock. If it's compromised, it can't run up a model bill.
    ]
  })
}

resource "aws_iam_role" "worker" {
  name                 = "triage-app-worker"
  assume_role_policy   = data.aws_iam_policy_document.lambda_trust.json
  permissions_boundary = local.app_boundary_arn
}

resource "aws_iam_role_policy" "worker" {
  role = aws_iam_role.worker.id
  name = "worker"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.worker.arn}:*" },
      { Effect = "Allow", Action = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility"], Resource = aws_sqs_queue.tickets.arn },
      { Effect = "Allow", Action = ["dynamodb:UpdateItem", "dynamodb:GetItem"], Resource = aws_dynamodb_table.tickets.arn },
      { Effect = "Allow", Action = ["bedrock:InvokeModel"], Resource = local.model_arn }, # one model, one Region
      { Effect = "Allow", Action = ["ssm:GetParameters"], Resource = [aws_ssm_parameter.langfuse_public_key.arn, aws_ssm_parameter.langfuse_secret_key.arn] },
    ]
  })
}

# --- Functions -------------------------------------------------------------------------------------------

resource "aws_lambda_function" "api" {
  function_name = "${local.name}-api"
  role          = aws_iam_role.api.arn
  runtime       = "python3.13"
  architectures = ["x86_64"]
  handler       = "triage.api.handler"
  s3_bucket     = aws_s3_object.lambda_zip.bucket
  s3_key        = aws_s3_object.lambda_zip.key
  memory_size   = 512
  timeout       = 15
  environment {
    variables = {
      TICKETS_TABLE = aws_dynamodb_table.tickets.name
      QUEUE_URL     = aws_sqs_queue.tickets.url
      API_KEY_PARAM = aws_ssm_parameter.api_key.name
    }
  }
  depends_on = [aws_cloudwatch_log_group.api, aws_iam_role_policy.api]
}

resource "aws_lambda_function_url" "api" {
  function_name = aws_lambda_function.api.function_name
  # Public HTTPS endpoint. Our own x-api-key check guards it (AWS_IAM auth would need SigV4
  # signing in every client, which is awkward for a browser review page).
  authorization_type = "NONE"
}

# Lets anyone reach the URL; our x-api-key check is the gate. (Reserved concurrency would cap
# abuse, but this account's limit is 10 concurrent executions and AWS keeps 10 unreserved,
# so nothing can be reserved. The account limit itself is the cap here.)
resource "aws_lambda_permission" "url_public" {
  statement_id           = "public-function-url"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.api.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}
resource "aws_lambda_function" "worker" {
  function_name = "${local.name}-worker"
  role          = aws_iam_role.worker.arn
  runtime       = "python3.13"
  architectures = ["x86_64"]
  handler       = "triage.worker.handler"
  s3_bucket     = aws_s3_object.lambda_zip.bucket
  s3_key        = aws_s3_object.lambda_zip.key
  memory_size   = 512
  timeout       = var.worker_timeout_s
  environment {
    variables = {
      TICKETS_TABLE             = aws_dynamodb_table.tickets.name
      BEDROCK_MODEL_ID          = var.model_id
      BEDROCK_RPM               = tostring(local.per_worker_rpm)
      LANGFUSE_HOST             = var.langfuse_host
      LANGFUSE_PUBLIC_KEY_PARAM = aws_ssm_parameter.langfuse_public_key.name
      LANGFUSE_SECRET_KEY_PARAM = aws_ssm_parameter.langfuse_secret_key.name
      APP_ENV                   = "prod"
      MAX_RECEIVES              = tostring(local.max_receives) # worker marks 'failed' on the last try
    }
  }
  depends_on = [aws_cloudwatch_log_group.worker, aws_iam_role_policy.worker]
}

resource "aws_lambda_event_source_mapping" "tickets" {
  event_source_arn        = aws_sqs_queue.tickets.arn
  function_name           = aws_lambda_function.worker.arn
  batch_size              = 1
  function_response_types = ["ReportBatchItemFailures"]
  scaling_config {
    maximum_concurrency = local.worker_concurrency # 2 workers × 9 req/min = 18 ≤ quota of 20
  }
}
