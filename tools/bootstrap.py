"""
Bootstrap script -- set up the project from local config files.

Reads .env, secrets.yml, and run_config.yml and generates:
- Settings files per server
- SSH key copies to the correct hook directories

Usage:
    python -m tools.bootstrap

Prerequisites:
    1. Copy .env.example to .env and fill in GCP project IDs, key paths
    2. Copy secrets.example.yml to secrets.yml and fill in passwords
    3. Copy run_config.example.yml to run_config.yml and fill in hostnames, IPs, MACs
    4. Place SSH keys in secrets/ directory (see secrets/ README)
"""

import shutil
import sys
from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
SECRETS_DIR = REPO_ROOT / "secrets"
HOOKS_DIR = REPO_ROOT / "dci-hooks"


def _check_prerequisites() -> list[str]:
    problems = []
    if not (REPO_ROOT / ".env").exists():
        problems.append(".env not found. Copy .env.example to .env and fill in values.")
    if not (REPO_ROOT / "secrets.yml").exists():
        problems.append("secrets.yml not found. Copy secrets.example.yml to secrets.yml and fill in passwords.")
    if not (REPO_ROOT / "run_config.yml").exists():
        problems.append("run_config.yml not found. Copy run_config.example.yml to run_config.yml and fill in values.")
    return problems


def _copy_ssh_keys() -> int:
    if not SECRETS_DIR.exists():
        print("  secrets/ directory not found -- skipping SSH key setup")
        return 0

    key_mappings = [
        ("dude_id_rsa", "dude/key/id_rsa"),
        ("dude_id_rsa.pub", "dude/key/id_rsa.pub"),
        ("repoadmin_id_rsa", "pbo/files/repoadmin_id_rsa"),
        ("repoadmin_id_rsa.pub", "pbo/files/repoadmin_id_rsa.pub"),
        ("repoadmin_id_rsa", "dude/benchmark/pbo/files/repoadmin_id_rsa"),
        ("repoadmin_id_rsa.pub", "dude/benchmark/pbo/files/repoadmin_id_rsa.pub"),
        ("pbench_id_rsa", "pbench/config/id_rsa"),
        ("ml4adm_id_rsa.pub", "dude/key/ml4adm_id_rsa.pub"),
    ]

    copied = 0
    for src_name, dst_rel in key_mappings:
        src = SECRETS_DIR / src_name
        dst = HOOKS_DIR / dst_rel
        if not src.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        dst.chmod(0o600)
        copied += 1
        print(f"  {src_name} -> {dst_rel}")

    return copied


def _generate_settings() -> int:
    try:
        with open(REPO_ROOT / "run_config.yml") as f:
            rc = yaml.safe_load(f) or {}
    except Exception as e:
        print(f"  Error loading run_config.yml: {e}")
        return 0

    servers = rc.get("servers", {})
    if not servers:
        print("  No servers defined in run_config.yml -- skipping settings generation")
        return 0

    print("  Settings are generated per-run via /dci-run <hostname> <topic>")
    print(f"  {len(servers)} server(s) configured: {', '.join(servers.keys())}")
    return 0


def _setup_pubsub(project_id: str) -> None:
    if not project_id:
        print("   GCP_PUBSUB_PROJECT_ID not set -- skipping Pub/Sub setup")
        return

    sa_key = None
    import os
    for var in ("PUBSUB_SA_KEY_PATH", "GOOGLE_APPLICATION_CREDENTIALS"):
        path = os.environ.get(var, "")
        if path and Path(path).exists():
            sa_key = path
            break

    if not sa_key:
        print("   No SA key found -- skipping Pub/Sub setup")
        return

    try:
        from google.cloud import pubsub_v1
        from google.oauth2 import service_account
    except ImportError:
        print("   google-cloud-pubsub not installed -- skipping Pub/Sub setup")
        return

    creds = service_account.Credentials.from_service_account_file(sa_key)
    publisher = pubsub_v1.PublisherClient(credentials=creds)
    subscriber = pubsub_v1.SubscriberClient(credentials=creds)

    topics = {
        "dci-commands": {"message_retention_duration": {"seconds": 600}},
        "dci-results": {"message_retention_duration": {"seconds": 600}},
    }
    subs = {
        "dci-commands-relay-sub": {"topic": "dci-commands", "ack_deadline_seconds": 600},
        "dci-results-agent-sub": {"topic": "dci-results", "ack_deadline_seconds": 120},
    }

    for name, cfg in topics.items():
        topic_path = publisher.topic_path(project_id, name)
        try:
            publisher.get_topic(request={"topic": topic_path})
            print(f"   Topic {name}: exists")
        except Exception:
            try:
                publisher.create_topic(
                    request={"name": topic_path, **cfg}
                )
                print(f"   Topic {name}: created")
            except Exception as e:
                print(f"   Topic {name}: error -- {e}")

    for name, cfg in subs.items():
        sub_path = subscriber.subscription_path(project_id, name)
        topic_path = publisher.topic_path(project_id, cfg["topic"])
        try:
            subscriber.get_subscription(request={"subscription": sub_path})
            print(f"   Subscription {name}: exists")
        except Exception:
            try:
                subscriber.create_subscription(
                    request={
                        "name": sub_path,
                        "topic": topic_path,
                        "ack_deadline_seconds": cfg["ack_deadline_seconds"],
                        "expiration_policy": {},
                    }
                )
                print(f"   Subscription {name}: created")
            except Exception as e:
                print(f"   Subscription {name}: error -- {e}")


def main():
    print("DCI Bootstrap")
    print("=" * 40)

    problems = _check_prerequisites()
    if problems:
        print("\nMissing prerequisites:")
        for p in problems:
            print(f"  - {p}")
        print("\nFix the above and re-run: python -m tools.bootstrap")
        sys.exit(1)

    print("\n1. Copying SSH keys from secrets/ to hooks directories...")
    n_keys = _copy_ssh_keys()
    print(f"   {n_keys} key(s) copied")

    print("\n2. Generating per-server settings files...")
    n_settings = _generate_settings()
    print(f"   {n_settings} settings file(s) generated")

    print("\n3. Verifying .env is loaded...")
    from dotenv import load_dotenv
    load_dotenv(REPO_ROOT / ".env")
    import os
    gcp = os.environ.get("GCP_PUBSUB_PROJECT_ID", "")
    vertex = os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID", "")
    print(f"   GCP_PUBSUB_PROJECT_ID: {'set' if gcp else 'NOT SET'}")
    print(f"   ANTHROPIC_VERTEX_PROJECT_ID: {'set' if vertex else 'NOT SET'}")

    print("\n4. Setting up GCP Pub/Sub...")
    _setup_pubsub(gcp)

    print("\nBootstrap complete.")
    print("Run: claude   (to start Claude Code)")
    print("Then: /dci-run <hostname> [topic]")


if __name__ == "__main__":
    main()
