package nl.minbzk.rig.keycloak.metrics;

import static org.junit.Assert.assertEquals;

import nl.minbzk.rig.keycloak.metrics.MetricsTokenGuard.Outcome;
import org.junit.Test;

/**
 * Toetst de vier gevallen van de tokencontrole op /rig-metrics.
 */
public class MetricsTokenGuardTest {

    private static final String TOKEN = "s3cr3t-scrape-token";

    private final MetricsTokenGuard guard = new MetricsTokenGuard(TOKEN);

    @Test
    public void zonderHeaderGeenToegang() {
        assertEquals(Outcome.UNAUTHORIZED, guard.check(null));
    }

    @Test
    public void metEenAnderSchemaGeenToegang() {
        assertEquals(Outcome.UNAUTHORIZED, guard.check("Basic " + TOKEN));
    }

    @Test
    public void metEenFoutTokenGeenToegang() {
        assertEquals(Outcome.UNAUTHORIZED, guard.check("Bearer niet-het-token"));
    }

    @Test
    public void metHetGoedeTokenWelToegang() {
        assertEquals(Outcome.ALLOWED, guard.check("Bearer " + TOKEN));
    }

    @Test
    public void zonderGeconfigureerdTokenIsHetEndpointDicht() {
        assertEquals(Outcome.TOKEN_NOT_CONFIGURED, new MetricsTokenGuard(null).check("Bearer " + TOKEN));
        assertEquals(Outcome.TOKEN_NOT_CONFIGURED, new MetricsTokenGuard("").check("Bearer " + TOKEN));
    }
}
