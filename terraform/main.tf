# mt5-hedge infrastructure, one apply:
#   s3rds       the database: the Registry module cloudformer/infra/aws//modules/s3rds (S3 bucket + Lambda +
#               Function URL + web console), deploying the s3rds release that module version was tested with
#   schema      the log table, created in the database (idempotent, re-run when mt5_hedge_version changes)
#   log-ingest  the public endpoint the Windows machines POST log rows to
# The log endpoint and the table come from the mt5-hedge release on PyPI (mt5_hedge.server), the same release the
# Windows machines install: sender, receiver and table change together, in one release.
module "s3rds" {
  source  = "cloudformer/infra/aws//modules/s3rds"
  version = "0.1.3" # deploys s3rds 0.3.5 by default

  project_name          = var.project_name # bucket <project_name>-db-<account id>, function <project_name>-db-lambda
  console_auth_username = var.db_user
  console_auth_password = var.db_password
  console               = var.console
  force_destroy         = var.force_destroy
  db_ro                 = false # one writer is all this needs: no read-only endpoint
}

# mt5-hedge==<mt5_hedge_version> from PyPI, the package only: mt5_hedge/server/{log_ingest.py, hedge_log.sql}
data "external" "mt5_hedge" {
  program = ["python3", "${path.module}/build.py", "mt5-hedge==${var.mt5_hedge_version}", "${path.module}/.build/mt5_hedge"]
}

resource "terraform_data" "schema" {
  triggers_replace = [var.mt5_hedge_version, var.db_name, module.s3rds.db_url]

  provisioner "local-exec" {
    command = "python3 ${path.module}/schema/apply_schema.py ${module.s3rds.db_url} ${var.db_name} ${data.external.mt5_hedge.result.dir}/mt5_hedge/server/hedge_log.sql"
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
  code_dir           = data.external.mt5_hedge.result.dir # the handler reads hedge_log.sql and sign_key.txt from the package
  sign_key_prev      = var.sign_key_prev

  depends_on = [terraform_data.schema]
}
