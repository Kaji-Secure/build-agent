#!/bin/sh
# Run the kaji builder agent on a node that has no Kubernetes cluster: make a single-node kind cluster there and install the agent into it.
#   KAJI_TOKEN=kb_... ./bootstrap-kind.sh [cluster name]       (the token of a builder made in the console: Organization -> Builders)
# Needs docker, kind, kubectl and helm. The cluster is named kaji-builder unless you give a name; KIND_NODE_IMAGE chooses the node image. Running it
# again upgrades the agent. Nothing else on the machine is touched: every command names the kind cluster's own context.
set -eu
name="${1:-kaji-builder}"
token="${KAJI_TOKEN:?set KAJI_TOKEN to the builder's token}"
url="${KAJI_URL:-wss://console.kaji.systems/agent/ws}"
image="${KAJI_AGENT_IMAGE:-registry.shift-computing.de/oz123/kaji-agent}"
tag="${KAJI_AGENT_TAG:-latest}"
here="$(cd "$(dirname "$0")" && pwd)"
for tool in docker kind kubectl helm; do
    command -v "$tool" >/dev/null || { echo "$tool is needed and was not found" >&2; exit 1; }
done

# A task mounts the Portage tree as an image, which Kubernetes does with the ImageVolume feature (beta in 1.33, so it must be switched on),
# and the container runtime of the node must support it: the node image below does.
node_image="${KIND_NODE_IMAGE:-kindest/node:v1.33.1}"
if ! kind get clusters 2>/dev/null | grep -qx "$name"; then
    kind create cluster --name "$name" --image "$node_image" --wait 120s --config - <<KINDCFG
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
featureGates:
  ImageVolume: true
KINDCFG
fi
ctx="kind-$name"
helm upgrade --install kaji-agent "$here/chart/kaji-agent" --kube-context "$ctx" --namespace kaji-agent --create-namespace \
    --set "token=$token" --set "url=$url" --set "image.repository=$image" --set "image.tag=$tag" --wait --timeout 180s
echo "the agent is running in $ctx: kubectl --context $ctx -n kaji-agent logs deploy/kaji-agent"
