package nl.minbzk.rig.keycloak.metrics;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;

/**
 * Toetst de header {@code Authorization: Bearer <token>} van een scrape tegen het token uit
 * de omgevingsvariabele {@code RIG_METRICS_TOKEN}.
 *
 * <p>Zonder dat token is het endpoint DICHT en niet open: een pod die de variabele niet
 * draagt geeft geen enkele metriek terug.
 */
public final class MetricsTokenGuard {

    /** De uitkomst van de controle, een op een met de statuscode die de resource teruggeeft. */
    public enum Outcome {
        ALLOWED,
        UNAUTHORIZED,
        TOKEN_NOT_CONFIGURED
    }

    static final String TOKEN_ENV_VAR = "RIG_METRICS_TOKEN";

    private static final String BEARER_PREFIX = "Bearer ";

    private final String expectedToken;

    MetricsTokenGuard(String expectedToken) {
        this.expectedToken = expectedToken;
    }

    public static MetricsTokenGuard fromEnvironment() {
        return new MetricsTokenGuard(System.getenv(TOKEN_ENV_VAR));
    }

    public Outcome check(String authorizationHeader) {
        if (expectedToken == null || expectedToken.isEmpty()) {
            return Outcome.TOKEN_NOT_CONFIGURED;
        }
        if (authorizationHeader == null || !authorizationHeader.startsWith(BEARER_PREFIX)) {
            return Outcome.UNAUTHORIZED;
        }
        String presented = authorizationHeader.substring(BEARER_PREFIX.length());
        // MessageDigest.isEqual stapt niet uit bij het eerste verschillende byte, zodat de
        // responstijd niets over het verwachte token prijsgeeft.
        boolean equal = MessageDigest.isEqual(
                presented.getBytes(StandardCharsets.UTF_8),
                expectedToken.getBytes(StandardCharsets.UTF_8));
        return equal ? Outcome.ALLOWED : Outcome.UNAUTHORIZED;
    }
}
