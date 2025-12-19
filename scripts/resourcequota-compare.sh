#!/bin/bash

# Script to display Capsule resource quota usage vs limits in a comparison table
# Usage: ./resourcequota-compare.sh <namespace>

NAMESPACE="${1:-}"

if [ -z "$NAMESPACE" ]; then
    echo "Usage: $0 <namespace>"
    echo "Example: $0 rig-prd-amt"
    exit 1
fi

# Check if namespace exists
if ! kubectl get namespace "$NAMESPACE" &>/dev/null; then
    echo "Error: Namespace '$NAMESPACE' not found"
    exit 1
fi

# Get resourcequota data
QUOTA_DATA=$(kubectl get resourcequota -n "$NAMESPACE" -o yaml 2>/dev/null | grep "quota.capsule.clastix.io")

if [ -z "$QUOTA_DATA" ]; then
    echo "No Capsule resource quota found in namespace '$NAMESPACE'"
    exit 1
fi

echo "$QUOTA_DATA" > /tmp/quota_$$.txt

echo ""
echo "Resource Quota for namespace: $NAMESPACE"
echo ""
echo "RESOURCE                        USED            HARD LIMIT"
echo "------------------------------  --------------  --------------"

grep "hard-" /tmp/quota_$$.txt | while IFS=: read key val; do
    resource=$(echo "$key" | sed "s/.*hard-//")
    hard_val=$(echo "$val" | tr -d " \"")
    used_val=$(grep "used-${resource}:" /tmp/quota_$$.txt | cut -d: -f2 | tr -d " \"")
    printf "%-30s  %14s  %14s\n" "$resource" "$used_val" "$hard_val"
done | sort

rm -f /tmp/quota_$$.txt
