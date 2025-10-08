We use asdf voor tools:

https://asdf-vm.com/guide/introduction.html

For local setup, a kind cluster is used with the Haven+ Flux installation:

https://gitlab.com/commonground/haven/havenplus/gitops-flux

Because ODCN uses ArgoCD, we need to [install that as well](https://argo-cd.readthedocs.io/en/stable/getting_started/). ODCN uses a multi-tenant ArgoCD setup, so we will mimic a similar approach.

The idea is:
- use a form
  - basic question is "a namspace"
  - the URL of your infra
  - rest is up to you
Or more complete.
  - Also define services to use (accounts will be created) Postgress / Minio
  - For future, also create backups when needed
  - For future, also support cloning a database/bucket for f.e. feature branches
  - Support SSO request, requires ingress, challenge: create realms and still use one SSO, shared and use secrets?

Challenge: using (namespaced) SOPS in ArgoCD, no native support for this like Flux has, so how to work around it.
Using a sidecard to use kustomize and sops. No image available with the correct tools, building our own.

Vault server: when startup, needs to be initialized and tokens need to be saved, needs to be automated?:
k exec -it vault-0 -- bin/sh
vault operator init

Remember: when working in local kind cluster, the ImagePullPolicy must be never and you must load images
into kind manually, see images folder.

Created docker image to work with KSOPS plugin

To reapply all Argo changes:
kubectl rollout restart deployment,statefulset -l app.kubernetes.io/part-of=argocd -n rig-system
task bootstrap-argo-system

k exec -it argocd-repo-server-b96988f-8pqsg -c cmp-server -- /bin/bash

```
# Download from: https://github.com/argoproj-labs/argocd-operator/releases
wget https://github.com/argoproj-labs/argocd-operator/archive/refs/tags/v0.14.0.tar.gz
tar -xzf v0.14.0.tar.gz
cd argocd-operator-0.14.0

kustomize build config/default | kubectl replace --force -f -
```

install kind
install gitopx-flux
install argocd operator
run bootstrap local


start GIT deamon in root folder (for ArgoCD to read the infrastructure)
git daemon \
--base-path=. \
--export-all \
--reuseaddr \
--informative-errors \
--verbose \
--enable=upload-pack \
--port=9090 \
--listen=0.0.0.0 \
--log-destination=stderr

Create a port forward so you can access ArgoCD:
kubectl port-forward svc/argocd-server -n rig-system 8080:80

connect to the database, make sure to use -h localhost:
kubectl exec -it rig-db-1 -n rig-system -- bash -c "PGPASSWORD='argocd' psql -h localhost -U argocd -d argocd"


Running a local test GIT server with Docker:
https://github.com/rockstorm101/git-server-docker
docker run --name git-test-server -d -v git-repositories:/srv/git -p 2222:22 rockstorm/git-server
docker stop git-test-server
docker start git-test-server

Added a config management plugin in argo because we want to support kustomize + sops:
https://akuity.io/blog/config-management-plugins

PLAN1:
- read basic project file
- creates a repo for the project file
- registers the project repo in the operations-manager
  - that creates a namespace
  - that creates a sops pair
  - that creates an (empty or demo) infra repo
- that adds that infra repo to our Argo as "Application"
- which is / gets deployed

PART2:
- UI for the project file

Configmap option idea:
- create configmap (or env file) in branch and sops encrypt it to GIT
- when calling API to deploy, parse the configmap file / content / path
- the API will combine the deployment with the right configmap... 


Wanting local DNS for nicer ingress:
https://mya.sh/blog/2020/10/21/local-ingress-domains-kind/
https://gist.github.com/ogrrd/5831371

Lets use:
https://hub.docker.com/r/nginxdemos/hello/


Load docker to kind:
https://akoserwal.medium.com/how-to-load-the-local-docker-image-in-kind-kubernetes-cluster-a21f0b8327ba
Argo configmap plugin:
https://akuity.io/blog/config-management-plugins
Using kustomize sops plugin:
https://github.com/viaduct-ai/kustomize-sops
Local GIT server:
https://github.com/rockstorm101/git-server-docker


After a clean kind cluster from haven common grounds:
https://gitlab.com/commonground/haven/havenplus

Install ArgoCD CDR (hacked):
task prepare-argocd-operator

Install the CMP Argo server:
kind load docker-image rig-cmp-argo-kustomize-sops:latest --name gitops-fluxcd
(should become a task?)

Install the RIG operator image to kind:
# TODO: to load the current known docker image, use below, should become a task
kind load docker-image operations-manager:latest --name gitops-fluxcd
# to build and update, we would run -> task update-operations-manager

ALSO: install the sops secret for Argo to use in the rig-system namspace.. this should be a task as well?
# see security/readme.md how to create the secret

Then bootstrap RIG system with Argo:
task bootstrap-argo-system



argo cd: kubectl port-forward svc/argocd-server -n rig-system 8080:80

git daemon \
--base-path=. \
--export-all \
--reuseaddr \
--informative-errors \
--verbose \
--enable=upload-pack --enable=receive-pack \
--port=9090 \
--listen=0.0.0.0 \
--log-destination=stderr

live-server --port=9093


Refresh argo application (refresh is trigger git retrieval):

export ARGOCD_TOKEN="eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJhcmdvY2QiLCJzdWIiOiJwcm9qOmRlZmF1bHQ6YXV0b21hdGlvbi1zZXJ2aWNlIiwibmJmIjoxNzUxMzYzMDgxLCJpYXQiOjE3NTEzNjMwODEsImp0aSI6Ijk0NGM2N2UwLTVkODItNDg5MC1iODE3LTdjMTRkZTIzY2Y3OSJ9.6-QhOIDAuTrc5plu617UVq-MRQ94l2OxyEW13nL-kmg"

# List applications
curl -X GET \
-H "Authorization: Bearer $ARGOCD_TOKEN" \
http://localhost:8080/api/v1/applications

# Refresh application
curl -X GET \
-H "Authorization: Bearer $ARGOCD_TOKEN" \
"http://localhost:8080/api/v1/applications/example-project-just-a-name?refresh=hard"

# Sync application
curl -X POST \
-H "Authorization: Bearer $ARGOCD_TOKEN" \
-H "Content-Type: application/json" \
http://localhost:8080/api/v1/applications/example-project-just-a-name/sync

curl -X POST "http://operations-manager.kind/api/git/repositories" \
  -H "Content-Type: application/json" \
  -H "X-API-Token: d68d6aebd694d636e5eb4784a952b9c3" \
  -d '{"repo_name": "main-repo-autocreated"}'


curl -X POST "http://operations-manager.kind/api/projects/process" \
  -H "Content-Type: application/json" \
  -H "X-API-Token: d68d6aebd694d636e5eb4784a952b9c3" \
  -d '{"project_file_path": "projects/simple-example.yaml"}'

curl -X POST "http://operations-manager.kind/api/projects/process" \
-H "Content-Type: application/json" \
-H "X-API-Token: d68d6aebd694d636e5eb4784a952b9c3" \
-d '{"project_file_path": "projects/beslishulp.yaml"}'

curl -X POST "http://operations-manager.rig.prd1.gn2.quattro.rijksapps.nl/api/projects/process" \
-H "Content-Type: application/json" \
-H "X-API-Token: d68d6aebd694d636e5eb4784a952b9c3" \
-d '{"project_file_path": "projects/robbert.yaml"}'




ODC steps:
- create namespace manually first (kustomize timing issue)
- apply kustomize argo cd
- create sops secret security/readme.md

Keep in mind:
current ODCN has limits on CPU (and memory probably).. 8 CPU
ArgoCD application controller needs a lot of memory, it crashes with too little, find out why, whole Argo is down then

Note:
in ODCN, we can not create the namespace with the label directly, it throws an error, instead at it later:
k label namespace rig-prd-robbert argocd.argoproj.io/managed-by=rig-prd-operations


curl -X POST "http://localhost:9595/api/projects/update-image" \
-H "Content-Type: application/json" \
-H "X-Project-API-Key: your-project-api-key-here" \
-d '{
"projectName": "example-project",
"componentName": "frontend",
"deploymentName": "staging",
"newImageUrl": "nginx:1.21"
}'


API Calls - Use "secret" as the API key:

# Update project image
curl -X PUT "http://localhost:9595/api/projects/rens2" \
-H "Content-Type: application/json" \
-H "X-Project-API-Key: secret" \
-d '{
"action": "update-image",
"deploymentName": "staging",
"componentName": "frontend",
"newImageUrl": "tutum/hello-world"
}'

curl -X DELETE "http://localhost:9595/api/projects/test-project" \
-H "Content-Type: application/json" \
-H "X-Project-API-Key: secret" \
-d '{"confirmDeletion": true}'

Deleting test namespaces:
kubectl get namespaces -o name | grep '^namespace/rig-' | grep -v 'rig-system' | xargs kubectl delete


We had to update the ingress buffer for the digilab keycloak:
https://stackoverflow.com/questions/56126864/why-do-i-get-502-when-trying-to-authenticate

# TODO
Deleting all argo resources (the application and project) in the same run does not seem to work,
a race condition where the application can not be deleted because the project is gone occurs,
maybe this also occurs with the setup which makes it so slow..
