# The database: s3rds (https://github.com/cloudformer/s3rds) — one SQLite file on S3 behind a Lambda Function URL.
# Vendored here so this repo deploys everything itself; the server code comes from the pip package (s3rds_version).
# Read/write endpoint only (one writer: reserved concurrency 1 + ETag compare-and-swap in the code).
locals {
  prefix        = "db/"
  function_name = "${var.project_name}-db-lambda"
}

# ---------------- S3: private, encrypted, TLS only ----------------
resource "aws_s3_bucket" "db" {
  bucket        = var.bucket
  force_destroy = var.force_destroy
}

resource "aws_s3_bucket_public_access_block" "db" {
  bucket                  = aws_s3_bucket.db.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "db" {
  bucket = aws_s3_bucket.db.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_policy" "db" {
  bucket = aws_s3_bucket.db.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.db.arn, "${aws_s3_bucket.db.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
  depends_on = [aws_s3_bucket_public_access_block.db]
}

# ---------------- logs + IAM: scoped to db/*.sqlite and its own log group ----------------
resource "aws_cloudwatch_log_group" "db" {
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
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.db.arn}:*" },
      { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject"], Resource = "${aws_s3_bucket.db.arn}/${local.prefix}*.sqlite" },
      { Effect = "Allow", Action = ["s3:ListBucket"], Resource = aws_s3_bucket.db.arn, Condition = { StringLike = { "s3:prefix" = ["${local.prefix}*"] } } }
    ]
  })
}

# ---------------- packaging: pip install s3rds[server] (Linux wheels) at plan time ----------------
data "external" "db_build" {
  program = ["python3", "${path.module}/build_server.py", var.s3rds_version]
}

data "archive_file" "db_code" {
  type        = "zip"
  source_dir  = data.external.db_build.result.dir
  output_path = "${path.module}/.build/db.zip"
}

# ---------------- Lambda + public Function URL (HTTP Basic inside) ----------------
resource "aws_lambda_function" "db" {
  function_name                  = local.function_name
  role                           = aws_iam_role.lambda.arn
  runtime                        = "python3.12"
  handler                        = "s3rds.server.lambda_function.lambda_handler"
  filename                       = data.archive_file.db_code.output_path
  source_code_hash               = data.archive_file.db_code.output_base64sha256
  timeout                        = 15
  memory_size                    = 256
  reserved_concurrent_executions = 1

  environment {
    variables = {
      DB_BUCKET   = aws_s3_bucket.db.bucket
      DB_PREFIX   = local.prefix
      DB_USER     = var.user
      DB_PASSWORD = var.password
      READONLY    = "0"
      CONSOLE     = var.console ? "1" : "0"
    }
  }

  depends_on = [aws_cloudwatch_log_group.db, aws_iam_role_policy.lambda]
}

resource "aws_lambda_function_url" "db" {
  function_name      = aws_lambda_function.db.function_name
  authorization_type = "NONE"
}

# Public URL = anyone may *reach* the Lambda; every request still needs HTTP Basic inside it.
resource "aws_lambda_permission" "url" {
  statement_id           = "AllowPublicFunctionUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.db.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

resource "aws_lambda_permission" "url_invoke" {
  statement_id             = "AllowPublicFunctionUrlInvoke"
  action                   = "lambda:InvokeFunction"
  function_name            = aws_lambda_function.db.function_name
  principal                = "*"
  invoked_via_function_url = true
}
