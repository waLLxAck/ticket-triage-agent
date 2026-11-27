# One-off bootstrap: run once from a laptop with `aws login` credentials.
#
#   terraform -chdir=infra/bootstrap init && terraform -chdir=infra/bootstrap apply
#
# It creates what has to exist *before* CI can deploy anything:
#   1. an S3 bucket for Terraform state (so CI and your laptop share one source of truth) and
#      for Lambda build artefacts;
#   2. GitHub's OIDC identity provider, so AWS can verify tokens that GitHub Actions presents;
#   3. two IAM roles that GitHub Actions can assume WITHOUT any stored AWS keys:
#        - triage-gha-eval: invoke the Bedrock model only (for the eval gate on PRs);
#        - triage-gha-deploy: manage this app's resources, only from the "production" GitHub
#          environment, which only accepts the main branch.
#
# This config's own state stays local (infra/bootstrap/terraform.tfstate, git-ignored). It's
# the chicken-and-egg layer: the state bucket can't store the state of the thing that creates it.

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 6.0" }
  }
}

variable "region" {
  type    = string
  default = "eu-north-1"
}

variable "github_repo" {
  type    = string
  default = "waLLxAck/ticket-triage-agent"
}

# GitHub signs an *immutable* subject that includes the owner and repository IDs, e.g.
# repo:waLLxAck@15944458/ticket-triage-agent@1474551602:pull_request. Find yours with
#   gh api repos/<owner>/<repo>/actions/oidc/customization/sub   (sub_claim_prefix)
# A name-only subject ("repo:waLLxAck/ticket-triage-agent:...") no longer matches. The IDs
# stop a deleted-and-recreated repo with the same name from inheriting this trust.
variable "github_sub_prefix" {
  type    = string
  default = "repo:waLLxAck@15944458/ticket-triage-agent@1474551602"
}

variable "model_id" {
  type    = string
  default = "amazon.nova-lite-v1:0"
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { project = "ticket-triage-agent", managed_by = "terraform", layer = "bootstrap" }
  }
}

data "aws_caller_identity" "me" {}

locals {
  account_id = data.aws_caller_identity.me.account_id
  model_arn  = "arn:aws:bedrock:${var.region}::foundation-model/${var.model_id}"
}

# --- 1. State and artefact bucket -----------------------------------------------------------------

resource "aws_s3_bucket" "state" {
  bucket        = "triage-tfstate-${local.account_id}"
  force_destroy = true # demo project: allow `terraform destroy` to clean up completely
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration { status = "Enabled" } # recover from a corrupted state file
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    id     = "expire-old-lambda-builds"
    status = "Enabled"
    filter { prefix = "lambda/" }
    expiration { days = 14 }
    noncurrent_version_expiration { noncurrent_days = 14 }
  }
}

# --- 2. GitHub OIDC identity provider -----------------------------------------------------------

resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

# The trust policy is the security boundary. `sub` is a claim GitHub signs into the token:
# "repo:<owner>/<repo>:ref:refs/heads/main" for pushes to main, "...:pull_request" for PRs,
# "...:environment:production" for a job that targets the production environment.
# AWS only issues credentials if the claim matches, so a fork or another repo gets nothing.
data "aws_iam_policy_document" "trust_eval" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values = [
        "${var.github_sub_prefix}:pull_request",
        "${var.github_sub_prefix}:ref:refs/heads/*",
      ]
    }
  }
}

data "aws_iam_policy_document" "trust_deploy" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      # A job that declares `environment: production` gets this subject. The GitHub environment
      # only accepts deployments from main, so this works out as main only and never a PR.
      values = ["${var.github_sub_prefix}:environment:production"]
    }
  }
}

# --- 3a. Eval role: may call the model, nothing else --------------------------------------------

resource "aws_iam_role" "gha_eval" {
  name                 = "triage-gha-eval"
  assume_role_policy   = data.aws_iam_policy_document.trust_eval.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy" "gha_eval" {
  role = aws_iam_role.gha_eval.id
  name = "invoke-one-model"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["bedrock:InvokeModel"]
      Resource = local.model_arn
    }]
  })
}

# --- 3b. Deploy role: manage this app's resources (names start with "triage-") ------------------

resource "aws_iam_role" "gha_deploy" {
  name                 = "triage-gha-deploy"
  assume_role_policy   = data.aws_iam_policy_document.trust_deploy.json
  max_session_duration = 3600
}

resource "aws_iam_role_policy" "gha_deploy" {
  role = aws_iam_role.gha_deploy.id
  name = "deploy-triage-stack"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "StateAndArtefacts"
        Effect = "Allow"
        # Object* covers tagging and ACL reads that Terraform does on every S3 object it manages
        # (found by the first CI deploys: AccessDenied on s3:GetObjectTagging, then s3:ListBucketVersions).
        Action   = ["s3:ListBucket*", "s3:GetBucketLocation", "s3:GetObject*", "s3:PutObject*", "s3:DeleteObject*"]
        Resource = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/*"]
      },
      {
        Sid    = "AppServices"
        Effect = "Allow"
        Action = ["lambda:*", "sqs:*", "dynamodb:*", "logs:*", "ssm:*", "cloudwatch:*", "sns:*"]
        Resource = [
          "arn:aws:lambda:${var.region}:${local.account_id}:function:triage-*",
          "arn:aws:sqs:${var.region}:${local.account_id}:triage-*",
          "arn:aws:dynamodb:${var.region}:${local.account_id}:table/triage-*",
          "arn:aws:logs:${var.region}:${local.account_id}:log-group:/aws/lambda/triage-*",
          "arn:aws:ssm:${var.region}:${local.account_id}:parameter/triage/*",
          "arn:aws:cloudwatch:${var.region}:${local.account_id}:alarm:triage-*",
          "arn:aws:sns:${var.region}:${local.account_id}:triage-*",
        ]
      },
      {
        Sid      = "ListAndDescribe" # these APIs don't support resource-level scoping
        Effect   = "Allow"
        Action   = ["lambda:GetAccountSettings", "lambda:ListEventSourceMappings", "lambda:GetEventSourceMapping", "lambda:ListTags", "lambda:TagResource", "logs:DescribeLogGroups", "ssm:DescribeParameters", "sqs:ListQueues", "budgets:*", "sts:GetCallerIdentity", "kms:Decrypt"]
        Resource = "*"
      },
      {
        Sid      = "OwnEventSourceMappingsOnly" # can't rewire or disable another app's consumers
        Effect   = "Allow"
        Action   = ["lambda:CreateEventSourceMapping", "lambda:UpdateEventSourceMapping", "lambda:DeleteEventSourceMapping"]
        Resource = "*"
        Condition = {
          ArnLike = { "lambda:FunctionArn" = "arn:aws:lambda:${var.region}:${local.account_id}:function:triage-*" }
        }
      },
      {
        Sid      = "ReadAppRoles"
        Effect   = "Allow"
        Action   = ["iam:GetRole", "iam:GetRolePolicy", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies", "iam:ListInstanceProfilesForRole", "iam:TagRole", "iam:UntagRole", "iam:DeleteRole", "iam:UpdateAssumeRolePolicy"]
        Resource = "arn:aws:iam::${local.account_id}:role/triage-app-*"
      },
      {
        # The privilege-escalation guard. Without it, a deploy could create "triage-app-x" with
        # an inline policy of Action "*", pass it to a Lambda, and run as account admin. Now a
        # role can only be created, or have its policies changed, if it carries the boundary,
        # and the boundary caps what any inline policy can grant.
        Sid      = "WriteAppRolesOnlyWithBoundary"
        Effect   = "Allow"
        Action   = ["iam:CreateRole", "iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:PutRolePermissionsBoundary"]
        Resource = "arn:aws:iam::${local.account_id}:role/triage-app-*"
        Condition = {
          StringEquals = { "iam:PermissionsBoundary" = aws_iam_policy.app_boundary.arn }
        }
      },
      {
        Sid      = "PassAppRolesToLambdaOnly"
        Effect   = "Allow"
        Action   = ["iam:PassRole"]
        Resource = "arn:aws:iam::${local.account_id}:role/triage-app-*"
        Condition = {
          StringEquals = { "iam:PassedToService" = "lambda.amazonaws.com" }
        }
      },
      {
        Sid      = "NeverRemoveTheBoundary"
        Effect   = "Deny"
        Action   = ["iam:DeleteRolePermissionsBoundary", "iam:CreatePolicyVersion", "iam:DeletePolicy", "iam:SetDefaultPolicyVersion"]
        Resource = "*"
      },
      {
        Sid      = "SmokeTestModel"
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel"]
        Resource = local.model_arn
      },
    ]
  })
}

# --- Permissions boundary for every app role ------------------------------------------------------
# The *maximum* any triage-app-* role can ever do, whatever its own policies say. Effective
# permissions = role policy ∩ boundary.

resource "aws_iam_policy" "app_boundary" {
  name        = "triage-app-boundary"
  description = "Ceiling for triage-app-* Lambda roles"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "arn:aws:logs:${var.region}:${local.account_id}:log-group:/aws/lambda/triage-*" },
      { Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Scan", "dynamodb:Query"], Resource = "arn:aws:dynamodb:${var.region}:${local.account_id}:table/triage-*" },
      { Effect = "Allow", Action = ["sqs:SendMessage", "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes", "sqs:ChangeMessageVisibility"], Resource = "arn:aws:sqs:${var.region}:${local.account_id}:triage-*" },
      { Effect = "Allow", Action = ["ssm:GetParameters"], Resource = "arn:aws:ssm:${var.region}:${local.account_id}:parameter/triage/*" },
      { Effect = "Allow", Action = ["bedrock:InvokeModel"], Resource = local.model_arn },
    ]
  })
}

output "app_boundary_arn" { value = aws_iam_policy.app_boundary.arn }
output "state_bucket" { value = aws_s3_bucket.state.bucket }
output "eval_role_arn" { value = aws_iam_role.gha_eval.arn }
output "deploy_role_arn" { value = aws_iam_role.gha_deploy.arn }
