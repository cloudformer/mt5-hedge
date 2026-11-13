# The public log endpoint: Lambda + Function URL. Validates the form and upserts into the database.
locals {
  function_name = "${var.project_name}-log-lambda"
}

# The zip = this module's code + the table definition (its column list is the validation whitelist).
data "archive_file" "code" {
  type        = "zip"
  output_path = "${path.module}/.build/log_ingest.zip"
  source {
    content  = file("${path.module}/log_ingest.py")
    filename = "log_ingest.py"
  }
  source {
    content  = var.schema_sql
    filename = "hedge_log.sql"
  }
}

resource "aws_cloudwatch_log_group" "ingest" {
  name              = "/aws/lambda/${local.function_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_iam_role" "lambda" {
  name = "${local.function_name}-role"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "lambda" {
  name = "${local.function_name}-policy"
  role = aws_iam_role.lambda.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.ingest.arn}:*" }]
  })
}

resource "aws_lambda_function" "ingest" {
  function_name    = local.function_name
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  handler          = "log_ingest.handler"
  filename         = data.archive_file.code.output_path
  source_code_hash = data.archive_file.code.output_base64sha256
  timeout          = 30
  memory_size      = 128
  architectures    = ["arm64"]

  environment {
    variables = {
      S3RDS_URL      = var.s3rds_query_url
      S3RDS_USER     = var.s3rds_user
      S3RDS_PASSWORD = var.s3rds_password
      SIGN_KEY       = var.sign_key
      SIGN_KEY_PREV  = var.sign_key_prev
    }
  }

  depends_on = [aws_cloudwatch_log_group.ingest, aws_iam_role_policy.lambda]
}

resource "aws_lambda_function_url" "ingest" {
  function_name      = aws_lambda_function.ingest.function_name
  authorization_type = "NONE" # public: the senders are throw-away machines with no credentials; the code checks format only
}

resource "aws_lambda_permission" "url" {
  statement_id           = "AllowPublicFunctionUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.ingest.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

resource "aws_lambda_permission" "url_invoke" {
  statement_id             = "AllowPublicFunctionUrlInvoke"
  action                   = "lambda:InvokeFunction"
  function_name            = aws_lambda_function.ingest.function_name
  principal                = "*"
  invoked_via_function_url = true
}
