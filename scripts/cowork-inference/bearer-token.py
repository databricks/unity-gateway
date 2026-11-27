"""Print a fresh Databricks M2M access token for Claude Desktop's credential helper.

UG runs this through DATABRICKS_BEARER_COMMAND on every token fetch, so the long Cowork
tasks never outlive the one-hour service-principal token.
"""

import base64
import json
import os
import sys
import urllib.parse
import urllib.request


def main():
    workspace = os.environ["UCODE_TEST_WORKSPACE"].rstrip("/")
    credentials = f"{os.environ['DATABRICKS_CLIENT_ID']}:{os.environ['DATABRICKS_CLIENT_SECRET']}"
    request = urllib.request.Request(
        f"{workspace}/oidc/v1/token",
        data=urllib.parse.urlencode(
            {"grant_type": "client_credentials", "scope": "all-apis"}
        ).encode(),
        headers={"Authorization": f"Basic {base64.b64encode(credentials.encode()).decode()}"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        print(json.load(response)["access_token"])


if __name__ == "__main__":
    sys.exit(main())
