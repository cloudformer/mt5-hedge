# mt5-hedge infrastructure, one apply:
#   s3rds       the database: S3 bucket + Lambda + Function URL + web console
#   schema      the log table, created in the database (idempotent, re-run when the .sql changes)
#   log-ingest  the public endpoint the Windows machines POST log rows to
module "s3rds" {
  source = "./s3rds"

  project_name       = var.project_name
  bucket             = var.db_bucket
  user               = var.db_user
  password           = var.db_password
  s3rds_version      = var.s3rds_version
  console            = var.console
  force_destroy      = var.force_destroy
  log_retention_days = var.log_retention_days
}

resource "terraform_data" "schema" {
  triggers_replace = [filesha256("${path.module}/schema/hedge_log.sql"), var.db_name, module.s3rds.db_url]

  provisioner "local-exec" {
    command = "python3 ${path.module}/schema/apply_schema.py ${module.s3rds.db_url} ${var.db_name} ${path.module}/schema/hedge_log.sql"
    environment = {
      DB_USER     = var.db_user
      DB_PASSWORD = var.db_password
    }
  }

  depends_on = [module.s3rds]
}

module "log_ingest" {
  source = "./log-ingest"

  project_name       = var.project_name
  s3rds_query_url    = "${module.s3rds.db_url}api/db/${var.db_name}/query"
  s3rds_user         = var.db_user
  s3rds_password     = var.db_password
  log_retention_days = var.log_retention_days
  schema_sql         = file("${path.module}/schema/hedge_log.sql")
  sign_key           = file("${path.module}/../mt5_hedge/sign_key.txt") # the key inside the PyPI package: one file, one source
  sign_key_prev      = var.sign_key_prev

  depends_on = [terraform_data.schema]
}
