"""Print a fresh Databricks M2M access token for Claude Desktop's credential helper.

UG runs this through DATABRICKS_BEARER_COMMAND on every token fetch, so the long Cowork
tasks never outlive the one-hour service-principal token.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.run_integration import mint_m2m_token  # noqa: E402


def main():
    print(
        mint_m2m_token(
            os.environ["UCODE_TEST_WORKSPACE"],
            os.environ["DATABRICKS_CLIENT_ID"],
            os.environ["DATABRICKS_CLIENT_SECRET"],
            timeout=10,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
