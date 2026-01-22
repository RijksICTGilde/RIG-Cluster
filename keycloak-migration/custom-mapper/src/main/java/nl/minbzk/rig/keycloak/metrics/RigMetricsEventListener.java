package nl.minbzk.rig.keycloak.metrics;

import org.jboss.logging.Logger;
import org.keycloak.events.Event;
import org.keycloak.events.EventListenerProvider;
import org.keycloak.events.admin.AdminEvent;
import org.keycloak.models.KeycloakSession;
import org.keycloak.models.RealmModel;

/**
 * Event listener that records authentication events for Prometheus metrics.
 */
public class RigMetricsEventListener implements EventListenerProvider {

    public static final String ID = "rig-metrics-listener";

    private static final Logger logger = Logger.getLogger(RigMetricsEventListener.class);
    private final KeycloakSession session;

    public RigMetricsEventListener(KeycloakSession session) {
        this.session = session;
    }

    @Override
    public void onEvent(Event event) {
        String realmName = getRealmName(event.getRealmId());
        String clientId = event.getClientId();
        String idpType = determineIdpType(event);

        logger.debugf("Recording event %s for realm %s, idp %s, client %s",
            event.getType(), realmName, idpType, clientId);

        switch (event.getType()) {
            case LOGIN -> PrometheusExporter.instance().recordLogin(realmName, idpType, clientId);
            case LOGIN_ERROR -> PrometheusExporter.instance().recordLoginError(
                realmName, idpType, event.getError(), clientId);
            case REGISTER -> PrometheusExporter.instance().recordRegistration(realmName, idpType, clientId);
            case LOGOUT, LOGOUT_ERROR -> PrometheusExporter.instance().recordLogout(realmName);
            default -> {
                // Other events not tracked for now
            }
        }
    }

    @Override
    public void onEvent(AdminEvent event, boolean includeRepresentation) {
        // Admin events not tracked for now
        logger.debugf("Admin event %s in realm %s (not tracked)",
            event.getOperationType(), event.getRealmId());
    }

    /**
     * Determine the identity provider type from the event.
     */
    private String determineIdpType(Event event) {
        if (event.getDetails() == null) {
            return PrometheusExporter.IDP_LOCAL;
        }

        // Check if identity_provider is set in event details
        String identityProvider = event.getDetails().get("identity_provider");
        if (identityProvider == null || identityProvider.isEmpty()) {
            return PrometheusExporter.IDP_LOCAL;
        }

        // Look up the IDP configuration to determine type
        RealmModel realm = session.realms().getRealm(event.getRealmId());
        if (realm != null) {
            var idp = realm.getIdentityProviderByAlias(identityProvider);
            if (idp != null) {
                String providerId = idp.getProviderId();
                if (providerId != null && providerId.contains("saml")) {
                    return PrometheusExporter.IDP_SAML;
                }
                return PrometheusExporter.IDP_OIDC;
            }
        }

        // Fallback: check if the alias contains hints
        if (identityProvider.toLowerCase().contains("saml")) {
            return PrometheusExporter.IDP_SAML;
        }

        return PrometheusExporter.IDP_OIDC;
    }

    /**
     * Get realm name from realm ID.
     */
    private String getRealmName(String realmId) {
        if (realmId == null) {
            return "unknown";
        }
        RealmModel realm = session.realms().getRealm(realmId);
        return realm != null ? realm.getName() : realmId;
    }

    @Override
    public void close() {
        // Nothing to close
    }
}
