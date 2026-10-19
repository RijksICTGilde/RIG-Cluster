# Bootstrap-templates voor odcn-staging

Staging leest andere repos dan productie: `zad-staging-argo-applications` en de private `zad-staging-secrets`. De infrastructuur komt uit de publieke RIG-Cluster-repo en heeft daarom geen credential nodig.

`task generate-bootstrap-secrets-for-cluster` neemt deze map wanneer `BOOTSTRAP_TEMPLATES_DIR` ernaar wijst (zie `.env-taskfile-odcn-staging`). De generator loopt niet recursief, dus de run voor productie ziet deze bestanden niet. Het wachtwoord is een token met leesrecht op die ene repo; vul het in met `task edit-secret`. Zie `features/zad-staging.md`.
