package nl.minbzk.rig.keycloak.metrics;

import jakarta.ws.rs.GET;
import jakarta.ws.rs.Produces;
import jakarta.ws.rs.core.MediaType;
import jakarta.ws.rs.core.Response;
import jakarta.ws.rs.ext.Provider;
import org.jboss.logging.Logger;
import org.keycloak.models.KeycloakSession;

/**
 * JAX-RS resource class for the metrics endpoint.
 * The @Provider annotation is required for RESTEasy in Keycloak Quarkus (v23+)
 * to properly recognize JAX-RS annotations on returned resource classes.
 * See: https://github.com/keycloak/keycloak/issues/25882
 */
@Provider
public class MetricsResource {

    private static final Logger logger = Logger.getLogger(MetricsResource.class);

    private final KeycloakSession session;

    public MetricsResource(KeycloakSession session) {
        this.session = session;
        logger.info("MetricsResource created with session");
    }

    @GET
    @Produces(MediaType.TEXT_PLAIN)
    public Response get() {
        logger.info("GET method called");
        try {
            String metrics = PrometheusExporter.instance().export(session);
            logger.info("Metrics exported, length: " + metrics.length());
            return Response.ok(metrics).type("text/plain; charset=utf-8").build();
        } catch (Exception e) {
            logger.error("Error exporting metrics", e);
            return Response.serverError().entity("Error: " + e.getMessage()).build();
        }
    }
}
