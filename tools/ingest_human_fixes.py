"""
Scan git history for human fixes to DCI hooks and ingest into the knowledge base.

Human fixes are commits that:
1. Touch files under dci-hooks_*/
2. Were NOT made by the DCI Agent (no "[agent-fix attempt N]" prefix)
3. Are not already recorded in the KB (deduplicated by commit SHA)

Usage:
    python -m tools.ingest_human_fixes [--repo-root /path/to/repo]
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

from agents.local.knowledge_base import ingest_human_fixes


def main():
    parser = argparse.ArgumentParser(description="Ingest human fixes from git into KB")
    parser.add_argument(
        "--repo-root", default=str(Path(__file__).resolve().parent.parent),
        help="Path to the git repo root",
    )
    args = parser.parse_args()

    result = ingest_human_fixes(repo_root=args.repo_root)
    print(json.dumps(result, indent=2))

    if not result.get("success"):
        sys.exit(1)


if __name__ == "__main__":
    main()
