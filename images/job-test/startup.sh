#!/bin/sh
# Tiny test workload for the job runner: prints the parameter it was given,
# then 10 numbered loglines (1s apart), then "done". Lets you verify a job
# image starts, streams logs, and completes.
set -e

echo "startup.sh started with parameter: ${*:-<none>}"

i=1
while [ "$i" -le 10 ]; do
  echo "logline $i"
  sleep 1
  i=$((i + 1))
done

echo "done"
