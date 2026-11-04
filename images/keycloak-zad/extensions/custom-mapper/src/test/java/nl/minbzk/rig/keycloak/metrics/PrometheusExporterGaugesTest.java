package nl.minbzk.rig.keycloak.metrics;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

import jakarta.persistence.EntityManager;
import jakarta.persistence.PersistenceException;
import jakarta.persistence.Query;
import java.util.Arrays;
import java.util.List;
import java.util.stream.Stream;
import org.junit.Test;
import org.keycloak.connections.jpa.JpaConnectionProvider;
import org.keycloak.models.KeycloakSession;
import org.keycloak.models.RealmModel;
import org.keycloak.models.RealmProvider;
import org.keycloak.models.UserProvider;

/**
 * What the gauge section reports when the users-by-IDP query breaks.
 *
 * <p>Until RC-411 a failing query fell back to counting every user as {@code local} and the
 * endpoint answered 200, which made a broken query indistinguishable from a realm without
 * federation. The JPA query runs against {@code keycloak-model-jpa}, a private API, so this
 * is the most likely thing to break on a version jump.
 */
public class PrometheusExporterGaugesTest {

    private static final String REALM = "rc411-realm";
    private static final int TOTAL_USERS = 7;

    @Test
    public void aBrokenQueryReportsAnErrorAndNoLocalCount() {
        KeycloakSession session = session(null);

        String output = PrometheusExporter.instance().export(session);

        assertTrue(output, output.contains(errorLine(1)));
        assertFalse(output, output.contains("rig_keycloak_users_by_idp_total{realm=\"" + REALM + "\""));
        assertTrue(output, output.contains("rig_keycloak_users_total{realm=\"" + REALM + "\"} " + TOTAL_USERS));
    }

    @Test
    public void aBrokenRealmListingReportsTheGaugesPartAndNoUserCounts() {
        RealmProvider realms = mock(RealmProvider.class);
        when(realms.getRealmsStream()).thenThrow(new RuntimeException("realm store unavailable"));

        KeycloakSession session = mock(KeycloakSession.class);
        when(session.realms()).thenReturn(realms);

        String output = PrometheusExporter.instance().export(session);

        assertTrue(output, output.contains(gaugesErrorLine(1)));
        assertFalse(output, output.contains("rig_keycloak_users_total{"));
    }

    @Test
    public void aSuccessfulCountReportsZeroAndLocalIsTheRemainder() {
        Query query = mock(Query.class);
        when(query.getResultList()).thenReturn(rows());

        String output = PrometheusExporter.instance().export(session(query));

        assertTrue(output, output.contains(errorLine(0)));
        assertTrue(output, output.contains(gaugesErrorLine(0)));
        assertTrue(output, output.contains(idpLine("sso-rijk", 4)));
        assertTrue(output, output.contains(idpLine("digid", 2)));
        // 7 total minus 6 federated.
        assertTrue(output, output.contains(idpLine(PrometheusExporter.IDP_LOCAL, 1)));
    }

    /** Two rows as the JPA query returns them: alias plus the count as a Long. */
    private static List<Object[]> rows() {
        return Arrays.asList(new Object[] {"sso-rijk", 4L}, new Object[] {"digid", 2L});
    }

    private static String errorLine(int value) {
        return PrometheusExporter.ERROR_METRIC
            + "{realm=\"" + REALM + "\",part=\"" + PrometheusExporter.PART_USERS_BY_IDP + "\"} " + value;
    }

    private static String gaugesErrorLine(int value) {
        return PrometheusExporter.ERROR_METRIC
            + "{realm=\"\",part=\"" + PrometheusExporter.PART_GAUGES + "\"} " + value;
    }

    private static String idpLine(String idp, int count) {
        return "rig_keycloak_users_by_idp_total{realm=\"" + REALM + "\",idp=\"" + idp + "\"} " + count;
    }

    /** A session with one non-master realm; a null query makes {@code createQuery} throw. */
    private static KeycloakSession session(Query query) {
        RealmModel realm = mock(RealmModel.class);
        when(realm.getName()).thenReturn(REALM);
        when(realm.getId()).thenReturn("id-" + REALM);

        RealmProvider realms = mock(RealmProvider.class);
        when(realms.getRealmsStream()).thenAnswer(invocation -> Stream.of(realm));

        UserProvider users = mock(UserProvider.class);
        when(users.getUsersCount(realm)).thenReturn(TOTAL_USERS);

        EntityManager em = mock(EntityManager.class);
        if (query == null) {
            when(em.createQuery(anyString()))
                .thenThrow(new PersistenceException("FederatedIdentityEntity is unknown"));
        } else {
            when(em.createQuery(anyString())).thenReturn(query);
        }

        JpaConnectionProvider jpa = mock(JpaConnectionProvider.class);
        when(jpa.getEntityManager()).thenReturn(em);

        KeycloakSession session = mock(KeycloakSession.class);
        when(session.realms()).thenReturn(realms);
        when(session.users()).thenReturn(users);
        when(session.getProvider(JpaConnectionProvider.class)).thenReturn(jpa);
        return session;
    }
}
