package nl.minbzk.rig.keycloak.metrics;

import org.jboss.logging.Logger;
import org.keycloak.Config;
import org.keycloak.events.EventListenerProvider;
import org.keycloak.events.EventListenerProviderFactory;
import org.keycloak.models.KeycloakSession;
import org.keycloak.models.KeycloakSessionFactory;
import org.keycloak.models.RealmModel;
import org.keycloak.models.utils.KeycloakModelUtils;
import org.keycloak.models.utils.PostMigrationEvent;
import org.keycloak.provider.ProviderEvent;

import java.util.HashSet;
import java.util.Set;

/**
 * Factory for creating RigMetricsEventListener instances.
 * Auto-registers the event listener on all realms at startup and on new realm creation.
 */
public class RigMetricsEventListenerFactory implements EventListenerProviderFactory {

    private static final Logger logger = Logger.getLogger(RigMetricsEventListenerFactory.class);

    @Override
    public EventListenerProvider create(KeycloakSession session) {
        return new RigMetricsEventListener(session);
    }

    @Override
    public void init(Config.Scope config) {
        // No configuration needed
    }

    @Override
    public void postInit(KeycloakSessionFactory factory) {
        // Register listener for post-migration (startup) to enable on all existing realms
        factory.register((ProviderEvent event) -> {
            if (event instanceof PostMigrationEvent) {
                KeycloakModelUtils.runJobInTransaction(factory, session -> {
                    session.realms().getRealmsStream().forEach(realm -> {
                        enableEventListenerIfNeeded(realm);
                    });
                });
            }
        });

        // Register listener for new realm creation
        factory.register((ProviderEvent event) -> {
            if (event instanceof RealmModel.RealmCreationEvent creationEvent) {
                enableEventListenerIfNeeded(creationEvent.getCreatedRealm());
            }
        });

        logger.info("RIG Metrics event listener factory initialized with auto-registration");
    }

    /**
     * Enable the metrics event listener on a realm if not already enabled.
     */
    private void enableEventListenerIfNeeded(RealmModel realm) {
        Set<String> listeners = realm.getEventsListenersStream().collect(java.util.stream.Collectors.toSet());

        if (!listeners.contains(RigMetricsEventListener.ID)) {
            Set<String> newListeners = new HashSet<>(listeners);
            newListeners.add(RigMetricsEventListener.ID);
            realm.setEventsListeners(newListeners);
            logger.infof("Auto-enabled RIG metrics event listener on realm: %s", realm.getName());
        }
    }

    @Override
    public void close() {
        // Nothing to close
    }

    @Override
    public String getId() {
        return RigMetricsEventListener.ID;
    }
}
