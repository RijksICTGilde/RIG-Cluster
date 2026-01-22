package nl.minbzk.rig.keycloak.metrics;

import jakarta.ws.rs.GET;
import jakarta.ws.rs.Produces;
import jakarta.ws.rs.core.MediaType;
import jakarta.ws.rs.core.Response;
import jakarta.ws.rs.ext.Provider;
import org.jboss.logging.Logger;
import org.keycloak.models.KeycloakSession;
import org.keycloak.services.resource.RealmResourceProvider;

/**
 * JAX-RS endpoint that exposes Prometheus metrics.
 * Available at /realms/{any-realm}/rig-metrics
 * Returns metrics for ALL realms regardless of which realm is in the URL.
 */
public class RigMetricsEndpoint implements RealmResourceProvider {

    private static final Logger logger = Logger.getLogger(RigMetricsEndpoint.class);

    public static final String ID = "rig-metrics";

    private final KeycloakSession session;

    public RigMetricsEndpoint(KeycloakSession session) {
        this.session = session;
        logger.info("RigMetricsEndpoint created");
    }

    @Override
    public Object getResource() {
        logger.info("getResource() called, returning MetricsResource");
        return new MetricsResource(session);
    }

    @Override
    public void close() {
        // Nothing to close
    }

    /**
     * Separate JAX-RS resource class for the metrics endpoint.
     * The @Provider annotation is required for RESTEasy in Keycloak Quarkus (v23+)
     * to properly recognize JAX-RS annotations on returned resource classes.
     * See: https://github.com/keycloak/keycloak/issues/25882
     */
    @Provider
    public static class MetricsResource {
        private static final Logger logger = Logger.getLogger(MetricsResource.class);

        private final KeycloakSession session;

        public MetricsResource(KeycloakSession session) {
            this.session = session;
            logger.info("MetricsResource created");
        }

        @GET
        @Produces(MediaType.TEXT_PLAIN)
        public Response get() {
            logger.info("GET /rig-metrics called");
            try {
                String metrics = PrometheusExporter.instance().export(session);
                logger.info("Metrics exported successfully, length: " + metrics.length());
                return Response.ok(metrics).build();
            } catch (Exception e) {
                logger.error("Error exporting metrics", e);
                return Response.serverError().entity("Error: " + e.getMessage()).build();
            }
        }
    }
}
