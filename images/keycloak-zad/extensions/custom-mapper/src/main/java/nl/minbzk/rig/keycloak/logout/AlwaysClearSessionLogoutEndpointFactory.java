package nl.minbzk.rig.keycloak.logout;

import org.keycloak.Config;
import org.keycloak.models.KeycloakSession;
import org.keycloak.models.KeycloakSessionFactory;
import org.keycloak.services.resource.RealmResourceProvider;
import org.keycloak.services.resource.RealmResourceProviderFactory;

/**
 * Factory that exposes {@link AlwaysClearSessionLogoutEndpoint} at
 * {@code /realms/{realm}/always-clear-session-logout/{idpAlias}}.
 *
 * <p>Discovered via {@code ServiceLoader} (FQCN listed in
 * {@code META-INF/services/...RealmResourceProviderFactory}). The endpoint is
 * dormant unless an IdP's {@code singleLogoutServiceUrl} points at it.
 */
public class AlwaysClearSessionLogoutEndpointFactory implements RealmResourceProviderFactory {

    @Override
    public RealmResourceProvider create(KeycloakSession session) {
        return new AlwaysClearSessionLogoutEndpoint(session);
    }

    @Override
    public void init(Config.Scope config) {
    }

    @Override
    public void postInit(KeycloakSessionFactory factory) {
    }

    @Override
    public void close() {
    }

    @Override
    public String getId() {
        return AlwaysClearSessionLogoutEndpoint.ID;
    }
}
