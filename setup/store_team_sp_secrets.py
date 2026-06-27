# Databricks notebook source
# MUST be run on a Single User cluster so the naifp IAM role is attached.
#
# PURPOSE: One-time setup to store naifp team SP credentials from Cerberus into
# a Databricks Secret Scope so the Rally app can authenticate as the
# team SP and write to the Unity Catalog volume.
#
# BEFORE RUNNING: replace "app/PASTE_NAIFP_SDB_HERE" below with the actual
# Cerberus SDB path for the naifp team. It typically follows the pattern:
#   app/<prefix>-sole-serviceprincipals/tokensnaifp
# Check with a teammate or the Nike platforms Cerberus self-service UI.

# COMMAND ----------

# %pip install "cerberus-python-client>=2"
# dbutils.library.restartPython()

# COMMAND ----------

from cerberus.client import CerberusClient
from databricks.sdk import WorkspaceClient
from databricks.sdk.service import workspace
from databricks.sdk.errors.platform import ResourceAlreadyExists

# TODO: replace this with your actual Cerberus SDB path before running
CERBERUS_SDB = "app/PASTE_NAIFP_SDB_HERE"
TEAM_NAME = "naifp"

tokens = CerberusClient("https://prod.cerberus.nikecloud.com").get_secrets_data(CERBERUS_SDB)

prod_client_id     = tokens[f"ServicePrincipal_{TEAM_NAME}_dataadmin_client_id"]
prod_client_secret = tokens[f"ServicePrincipal_{TEAM_NAME}_dataadmin_client_secret"]
test_client_id     = tokens[f"ServicePrincipal_{TEAM_NAME}_developer_client_id"]
test_client_secret = tokens[f"ServicePrincipal_{TEAM_NAME}_developer_client_secret"]

assert all([prod_client_id, prod_client_secret, test_client_id, test_client_secret]), \
    "Cerberus returned empty values — check the SDB path and IAM role access"

# COMMAND ----------

ws = WorkspaceClient()
scope = f"team-{TEAM_NAME}-sp-secrets"

try:
    ws.secrets.create_scope(scope)
    print(f"Created new secret scope: {scope}")
except ResourceAlreadyExists:
    print(f"Secret scope already exists: {scope} — updating secrets in place")

ws.secrets.put_secret(scope=scope, key="prod_client_id",     string_value=prod_client_id)
ws.secrets.put_secret(scope=scope, key="prod_client_secret", string_value=prod_client_secret)
ws.secrets.put_secret(scope=scope, key="test_client_id",     string_value=test_client_id)
ws.secrets.put_secret(scope=scope, key="test_client_secret", string_value=test_client_secret)

ws.secrets.put_acl(scope=scope, principal=f"App.NikeSole.{TEAM_NAME}.Developer", permission=workspace.AclPermission.MANAGE)
ws.secrets.put_acl(scope=scope, principal=f"App.NikeSole.{TEAM_NAME}.DataAdmin", permission=workspace.AclPermission.MANAGE)

print(f"Success — scope '{scope}' contains: prod_client_id, prod_client_secret, test_client_id, test_client_secret")
