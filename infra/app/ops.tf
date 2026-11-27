# --- Operations: know when something is wrong, and when money is being spent ---------------------

resource "aws_sns_topic" "alerts" {
  name = "triage-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email # AWS emails a confirmation link; alerts start once it's clicked
}

# Any message in the dead-letter queue is a ticket that failed 3 times: a human must look at it.
resource "aws_cloudwatch_metric_alarm" "dlq_not_empty" {
  alarm_name          = "triage-dlq-not-empty"
  alarm_description   = "A ticket failed triage 3 times and is in the dead-letter queue"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.dlq.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

# Worker errors (exceptions, timeouts) over 5 minutes.
resource "aws_cloudwatch_metric_alarm" "worker_errors" {
  alarm_name          = "triage-worker-errors"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.worker.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
}

# A $1/month budget: email at 50% actual and 100% forecast. The Free plan can't bill you, but
# credits are finite, and this is the habit every client account needs.
resource "aws_budgets_budget" "monthly" {
  name         = "triage-monthly"
  budget_type  = "COST"
  limit_amount = "1"
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 50
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
  }
}
