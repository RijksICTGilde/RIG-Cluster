package nl.minbzk.rig.keycloak.metrics;

import jakarta.ws.rs.GET;
import jakarta.ws.rs.Produces;
import jakarta.ws.rs.core.HttpHeaders;
import jakarta.ws.rs.core.MediaType;
import jakarta.ws.rs.core.Response;
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
        logger.debug("RigMetricsEndpoint created");
    }

    @Override
    public Object getResource() {
        return new MetricsResource(session);
    }

    @Override
    public void close() {
        // Nothing to close
    }

    /**
     * Separate JAX-RS resource class for the metrics endpoint.
     *
     * <p>Deliberately NOT annotated {@code @Provider}: in Keycloak 26 that annotation marks
     * ROOT resources (see {@code RealmsResource}), while a class returned by a sub-resource
     * locator carries only its JAX-RS annotations, as Keycloak's own {@code DeviceEndpoint}
     * does. Up to 25 the annotation was needed here and 26.2 turned it into a 404.
     */
    public static class MetricsResource {
        private static final Logger logger = Logger.getLogger(MetricsResource.class);

        private final KeycloakSession session;

        public MetricsResource(KeycloakSession session) {
            this.session = session;
        }

        @GET
        @Produces(MediaType.TEXT_PLAIN)
        public Response get() {
            String authorization = session.getContext().getRequestHeaders()
                    .getHeaderString(HttpHeaders.AUTHORIZATION);
            MetricsTokenGuard.Outcome outcome = MetricsTokenGuard.fromEnvironment().check(authorization);
            if (outcome != MetricsTokenGuard.Outcome.ALLOWED) {
                return refuse(outcome);
            }

            logger.debug("GET /rig-metrics called");
            try {
                String metrics = PrometheusExporter.instance().export(session);
                logger.debugf("Metrics exported successfully, length: %d", metrics.length());
                return Response.ok(metrics).build();
            } catch (Exception e) {
                logger.error("Error exporting metrics", e);
                return Response.serverError().entity("Error: " + e.getMessage()).build();
            }
        }

        /** Geen enkele metriek in het antwoord, en geen hint over het token in de log. */
        private static Response refuse(MetricsTokenGuard.Outcome outcome) {
            if (outcome == MetricsTokenGuard.Outcome.TOKEN_NOT_CONFIGURED) {
                logger.warnf("%s is niet gezet, dus /rig-metrics geeft geen metrieken",
                        MetricsTokenGuard.TOKEN_ENV_VAR);
                return refusal(Response.Status.SERVICE_UNAVAILABLE);
            }
            logger.warn("Scrape van /rig-metrics geweigerd: ontbrekend of onjuist Bearer-token");
            return refusal(Response.Status.UNAUTHORIZED);
        }

        /**
         * Het mediatype staat erop omdat Keycloaks DefaultSecurityHeadersProvider een antwoord
         * zonder mediatype afbreekt, en de 503 op de sandbox zo als 500 met JSON-body aankwam.
         */
        private static Response refusal(Response.Status status) {
            return Response.status(status).type(MediaType.TEXT_PLAIN).build();
        }
    }
}
