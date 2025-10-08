When deploying to ODCN, the namespace creation takes a bit longer so the apply for kustomize fails
for other sources. Maybe the namespace must be a separate step after all.

The user applications app is for now also in this repo. This is also in the cluster-specific repo,
as that is where we would configure infra setup which this might be as well.
To reduce complexity, we keep it in bootstrap for now.
