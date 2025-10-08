The idea with services would be:

1. production like: create your own services, single deployment user
2. in-control-and-seperated-but-shared: create your own services per namespace, but shared between deployments in that namespace
3. fully-shared: the services run in a system namespace, you use them

