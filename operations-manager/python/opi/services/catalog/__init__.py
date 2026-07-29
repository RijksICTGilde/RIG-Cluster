"""Service catalog -- one module per platform service.

A *service* is a user-facing, configuration-as-code building block that a project
declares in its YAML (keycloak, postgresql-database, ...). It is NOT a *connector*
or *provider* (which are "how OPI talks to an external system"). Each service's
behaviour lives in its own module here; ``opi.services.registry`` assembles them
into ``SERVICES``. Config models and config UI move in per service as they migrate.
"""
