variable "region" {
  type    = string
  default = "eu-north-1"
}

variable "model_id" {
  type    = string
  default = "amazon.nova-lite-v1:0"
}

variable "artifacts_bucket" {
  type        = string
  description = "Bucket for Lambda zips (the bootstrap state bucket)"
}

variable "lambda_zip" {
  type    = string
  default = "../../build/lambda.zip"
}

variable "worker_timeout_s" {
  type    = number
  default = 120
}

variable "langfuse_host" {
  type    = string
  default = "https://cloud.langfuse.com"
}

variable "langfuse_public_key" {
  type      = string
  sensitive = true
}

variable "langfuse_secret_key" {
  type      = string
  sensitive = true # never printed in plan output
}

variable "alert_email" {
  type      = string
  sensitive = true
}

output "api_url" { value = aws_lambda_function_url.api.function_url }
output "queue_url" { value = aws_sqs_queue.tickets.url }
output "tickets_table" { value = aws_dynamodb_table.tickets.name }
output "api_key" {
  value     = random_password.api_key.result
  sensitive = true # read it with: terraform output -raw api_key
}
