#!/bin/bash

# Script to list all pods with their resource requests/limits across all namespaces
# and calculate totals at the end

echo "Gathering pod resource information across all namespaces..."
echo ""

for ns in $(kubectl get namespaces -o jsonpath='{.items[*].metadata.name}'); do
    pods=$(kubectl get pods -n "$ns" -o jsonpath='{.items[*].metadata.name}' 2>/dev/null)

    if [ -n "$pods" ]; then
        echo "=== Namespace: $ns ==="

        kubectl get pods -n "$ns" -o jsonpath='{range .items[*]}Pod: {.metadata.name}{"\n"}{range .spec.containers[*]}  Container: {.name}{"\n"}    CPU Request: {.resources.requests.cpu}{"\n"}    CPU Limit: {.resources.limits.cpu}{"\n"}    Memory Request: {.resources.requests.memory}{"\n"}    Memory Limit: {.resources.limits.memory}{"\n"}{end}{"\n"}{end}'
        echo ""
    fi
done

echo "=========================================="
echo "=== CLUSTER TOTALS ==="
echo "=========================================="

kubectl get pods --all-namespaces -o jsonpath='{range .items[*]}{range .spec.containers[*]}{.resources.requests.cpu}{" "}{.resources.limits.cpu}{" "}{.resources.requests.memory}{" "}{.resources.limits.memory}{"\n"}{end}{end}' | \
awk '
BEGIN { cpu_req=0; cpu_lim=0; mem_req=0; mem_lim=0 }
{
    # Convert CPU (handle m suffix and plain numbers)
    if ($1 ~ /m$/) { cpu_req += substr($1, 1, length($1)-1) }
    else if ($1 != "" && $1 ~ /^[0-9.]+$/) { cpu_req += $1 * 1000 }

    if ($2 ~ /m$/) { cpu_lim += substr($2, 1, length($2)-1) }
    else if ($2 != "" && $2 ~ /^[0-9.]+$/) { cpu_lim += $2 * 1000 }

    # Convert Memory (handle Ki, Mi, Gi, K, M, G suffixes and plain bytes)
    if ($3 ~ /Ki$/) { mem_req += substr($3, 1, length($3)-2) / 1024 }
    else if ($3 ~ /Mi$/) { mem_req += substr($3, 1, length($3)-2) }
    else if ($3 ~ /Gi$/) { mem_req += substr($3, 1, length($3)-2) * 1024 }
    else if ($3 ~ /K$/) { mem_req += substr($3, 1, length($3)-1) / 1024 }
    else if ($3 ~ /M$/) { mem_req += substr($3, 1, length($3)-1) }
    else if ($3 ~ /G$/) { mem_req += substr($3, 1, length($3)-1) * 1024 }
    else if ($3 != "" && $3 ~ /^[0-9]+$/) { mem_req += $3 / 1048576 }

    if ($4 ~ /Ki$/) { mem_lim += substr($4, 1, length($4)-2) / 1024 }
    else if ($4 ~ /Mi$/) { mem_lim += substr($4, 1, length($4)-2) }
    else if ($4 ~ /Gi$/) { mem_lim += substr($4, 1, length($4)-2) * 1024 }
    else if ($4 ~ /K$/) { mem_lim += substr($4, 1, length($4)-1) / 1024 }
    else if ($4 ~ /M$/) { mem_lim += substr($4, 1, length($4)-1) }
    else if ($4 ~ /G$/) { mem_lim += substr($4, 1, length($4)-1) * 1024 }
    else if ($4 != "" && $4 ~ /^[0-9]+$/) { mem_lim += $4 / 1048576 }
}
END {
    printf "Total CPU Requests:    %8.0fm  (%6.2f cores)\n", cpu_req, cpu_req/1000
    printf "Total CPU Limits:      %8.0fm  (%6.2f cores)\n", cpu_lim, cpu_lim/1000
    printf "Total Memory Requests: %8.0fMi (%6.2f Gi)\n", mem_req, mem_req/1024
    printf "Total Memory Limits:   %8.0fMi (%6.2f Gi)\n", mem_lim, mem_lim/1024
}'
