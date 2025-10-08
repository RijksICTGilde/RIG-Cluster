I want to build a operations manager application, which should perform certain workflow tasks for deployment to kubernetes. It should be able to do, for now,  the following:

- read a project yaml file
- have an API interface so calls to it can be made to perform certain actions
- create manifests based on helm (go template)
- create PRs on Github, Gitlab or Git
- make commits to Githib, Gitlab or Git
- monitor Github or Gitlab branches for changes
- use webhook integrations from Github or Gitlab to trigger actions
- create a namespace
- make API calls to systems like Keycloak, Postgress and f.e. Minio

I am used to writing in Python, but think using Go might be a solution for this as well. How difficult would it be 



Tech how:
- form:
  - select cluster
  - project name (= unique, will be namespace)
  - give infra repo or create new
  - infra type: (kustomize or kustomize with sops?)
-> on cluster: create namespace
  - add namespace to argo project
  - create argo application to infa url


To build the Docker image, you can run:

Because we need to copy the keys folder, run this command from the project root:

docker build -t operations-manager -f operations-manager/Dockerfile .

Copy the image to the kind cluster:
kind load docker-image operations-manager:latest --name gitops-fluxcd

To run the container with the environment variables from the .env file:
# docker run --env-file .env -p 8000:8000 opi-operations-manager
