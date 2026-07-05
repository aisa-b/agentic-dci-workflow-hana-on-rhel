#!/usr/bin/env bash
set -euo pipefail

# GCP Pub/Sub infrastructure setup for the DCI Multi-Agent system.
# Run once to create topics, subscriptions, and (optionally) a service account.
#
# IMPORTANT: This uses the Pub/Sub project (<your-pubsub-project>), NOT the Vertex AI project.
# Claude/Vertex AI uses <your-vertex-project> -- that is configured separately.
#
# Prerequisites:
#   - gcloud CLI authenticated (`gcloud auth login`)
#   - Access to the Pub/Sub GCP project
#
# Usage:
#   export GCP_PUBSUB_PROJECT_ID="<your-pubsub-project>"
#   bash infra/setup_pubsub.sh

PROJECT_ID="${GCP_PUBSUB_PROJECT_ID:?Set GCP_PUBSUB_PROJECT_ID before running this script}"

COMMANDS_TOPIC="dci-commands"
RESULTS_TOPIC="dci-results"
COMMANDS_SUB="dci-commands-relay-sub"
RESULTS_SUB="dci-results-agent-sub"

echo "=== DCI Agent Pub/Sub Setup ==="
echo "Pub/Sub Project: $PROJECT_ID"
echo "NOTE: This is the Pub/Sub project only. Claude/Vertex AI uses a separate project."
echo ""

gcloud config set project "$PROJECT_ID"

echo "--- Enabling Pub/Sub API ---"
gcloud services enable pubsub.googleapis.com

echo "--- Creating topics ---"
gcloud pubsub topics create "$COMMANDS_TOPIC" \
    --message-retention-duration=600s \
    2>/dev/null || echo "  Topic $COMMANDS_TOPIC already exists"

gcloud pubsub topics create "$RESULTS_TOPIC" \
    --message-retention-duration=600s \
    2>/dev/null || echo "  Topic $RESULTS_TOPIC already exists"

echo "--- Creating subscriptions ---"
gcloud pubsub subscriptions create "$COMMANDS_SUB" \
    --topic="$COMMANDS_TOPIC" \
    --ack-deadline=600 \
    --message-retention-duration=600s \
    --expiration-period=never \
    2>/dev/null || echo "  Subscription $COMMANDS_SUB already exists"

gcloud pubsub subscriptions create "$RESULTS_SUB" \
    --topic="$RESULTS_TOPIC" \
    --ack-deadline=120 \
    --message-retention-duration=600s \
    --expiration-period=never \
    2>/dev/null || echo "  Subscription $RESULTS_SUB already exists"

echo ""
echo "=== Setup Complete ==="
echo ""
echo "Environment variables to set:"
echo "  GCP_PUBSUB_PROJECT_ID=\"$PROJECT_ID\"   # Pub/Sub only"
echo "  PUBSUB_COMMANDS_TOPIC=\"$COMMANDS_TOPIC\""
echo "  PUBSUB_RESULTS_TOPIC=\"$RESULTS_TOPIC\""
echo "  PUBSUB_COMMANDS_SUB=\"$COMMANDS_SUB\""
echo "  PUBSUB_RESULTS_SUB=\"$RESULTS_SUB\""
echo ""
echo "For the relay on the remote network, you will also need a service account."
echo "Run: bash infra/setup_relay_sa.sh"
