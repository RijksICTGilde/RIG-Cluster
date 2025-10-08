For my company, I want to make Kubernetes deployment easy to any platform. Some platforms have other options and limitations than others. Some may have a predefined set of operators, others are more flexible, some may require extra security measures like port forwards. I want to focus on the minimal. If possible, I want to keep as much as possible as "infra as code", as this latter is implementation dependent, I am thinking of creating an abstraction layer, this may be similar to crossplane, so check my thoughts against that.

What I would want is to define a project in a yaml file, for example:

projects:
name: robbert1
services:
- name: postgress
user: postgresuser1
schema: schema1
- name: minio
user: miniouser1
bucket: my-bucket
deployments:
- name: frontend
base-image: some.storage/frontend
tag: v1
requests:
cpu: 1
memory: 1Gb
ports: 8080
base-ingress: www.myapp.example.com
keycloak-SSO: true
- name: backend
base-image: some.storage/backend
tag: v1
ports: 8080
base-ingress: www.myapp.example.com/api


There should/would be friendly UI to manage it. Maybe it also should contain the target cluster, as the ingress name may depend on that, or maybe there should be a cluster specific implementation.yaml which would manage such things; Also, I am uncertain where to keep the base image versions, as this would be updated by a CI/CD pipeline. So, there are several layers to be stacked which could make this overcomplicated.

However. If applied correctly, we could write an implementation Application, that could render the required manifests that can be synced with ArgoCD. Also, this implementation would need to run in the target cluster, so it can also manage resources like Keycloak or Minio or Postgress to do proper schema, user and realm management. Much like an operator, without being an actual operator.

Ofcourse there is the challenge of syncing and watching this; implementation Application would have to watch projects files. Also, when changing project files, I wonder how a diff would work to know what was modified or deleted. Also, I wonder if a project file would be a place to keep feature deployments, or if this should yet be another file. Also there is a difference in staging and production and seperation of concerns. I do think our own implementation could generate templates, f.e. using helm and create PR/MR to repo's so that ArgoCD can sync them, keeping the option of applying kustomize overlays so users can stil override or add things.