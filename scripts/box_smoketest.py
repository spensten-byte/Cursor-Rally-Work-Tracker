"""One-off smoke test: prove Databricks can authenticate to a Box tenant
with a JWT app and upload a file to the given folder.

Run this from a Databricks notebook (NOT locally — it needs the workspace's
ambient auth to read the box-*-* secrets, and Box's API is not reachable
from behind a corporate proxy that does TLS interception, which is the case
on Nike-managed laptops):

    %pip install "boxsdk[jwt]>=10"
    dbutils.library.restartPython()

    # then, in a NEW cell (packages only load after the restart):
    # %run this file, or paste its contents into a cell.

This is a standalone, throwaway script. It is not imported by the Rally
app and touches no app state — it only reads secrets from the
`pase-work-tracker` scope and calls the Box API directly.

Built against `boxsdk>=10`, which ships under the `box_sdk_gen` import
namespace. Box retired the classic `from boxsdk import JWTAuth, Client`
API in the v10 consolidation — see
https://github.com/box/box-python-sdk/blob/main/migration-guides/from-boxsdk-to-box_sdk_gen.md
"""

from databricks.sdk import WorkspaceClient
from box_sdk_gen import BoxClient, BoxJWTAuth, JWTConfig, UploadFileAttributes, UploadFileAttributesParentField
import io
from datetime import datetime, timezone

SCOPE = "pase-work-tracker"

# Flip between dev and prod by changing the key prefix + folder id below.
KEY_PREFIX = "box-dev"  # or "box-prod"
FOLDER_ID = "398011195047"  # dev folder; prod is 397702842180


def _read(ws: WorkspaceClient, key: str) -> str:
    try:
        return ws.dbutils.secrets.get(scope=SCOPE, key=key)
    except Exception:
        import base64
        r = ws.secrets.get_secret(scope=SCOPE, key=key)
        return base64.b64decode(r.value).decode("utf-8")


def main() -> None:
    ws = WorkspaceClient()

    jwt_config = JWTConfig(
        client_id=_read(ws, f"{KEY_PREFIX}-client-id"),
        client_secret=_read(ws, f"{KEY_PREFIX}-client-secret"),
        jwt_key_id=_read(ws, f"{KEY_PREFIX}-public-key-id"),
        private_key=_read(ws, f"{KEY_PREFIX}-private-key"),
        private_key_passphrase=_read(ws, f"{KEY_PREFIX}-passphrase"),
        enterprise_id=_read(ws, f"{KEY_PREFIX}-enterprise-id"),
    )
    auth = BoxJWTAuth(config=jwt_config)
    client = BoxClient(auth=auth)

    me = client.users.get_user_me()
    print(f"Authenticated as service account: {me.login} (id={me.id})")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    body = f"Hello from Databricks -- Rally Box smoke test at {stamp}\n".encode()
    file_name = f"rally_smoketest_{stamp}.txt"
    files = client.uploads.upload_file(
        UploadFileAttributes(name=file_name, parent=UploadFileAttributesParentField(id=FOLDER_ID)),
        io.BytesIO(body),
    )
    uploaded = files.entries[0]
    print(f"SUCCESS  id={uploaded.id}  name={uploaded.name}")


if __name__ == "__main__":
    main()
