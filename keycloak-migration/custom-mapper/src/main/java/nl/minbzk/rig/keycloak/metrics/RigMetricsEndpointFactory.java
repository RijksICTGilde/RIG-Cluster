package nl.minbzk.rig.keycloak.metrics;

import org.keycloak.Config;
import org.keycloak.models.KeycloakSession;
import org.keycloak.models.KeycloakSessionFactory;
import org.keycloak.services.resource.RealmResourceProvider;
import org.keycloak.services.resource.RealmResourceProviderFactory;

/**
 * Factory for creating RigMetricsEndpoint instances.
 * Registers the endpoint at /realms/{realm}/rig-metrics
 */
public class RigMetricsEndpointFactory implements RealmResourceProviderFactory {

    @Override
    public RealmResourceProvider create(KeycloakSession session) {
        return new RigMetricsEndpoint(session);
    }

    @Override
    public void init(Config.Scope config) {
        // No configuration needed
    }

    @Override
    public void postInit(KeycloakSessionFactory factory) {
        // Nothing to do
    }

    @Override
    public void close() {
        // Nothing to close
    }

    @Override
    public String getId() {
        return RigMetricsEndpoint.ID;
    }
}
