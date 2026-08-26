package nl.minbzk.rig.keycloak.email;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertThrows;
import static org.junit.Assert.assertTrue;

import java.util.HashMap;
import java.util.Map;
import java.util.Properties;
import org.junit.Test;

/**
 * Toetst de EEN eigenschap waar deze provider om draait: de bestemming komt uit de omgeving
 * en NIET uit de {@code smtpServer} van de realm.
 */
public class RelayEmailSenderProviderTest {

    private static Map<String, String> omgeving() {
        Map<String, String> env = new HashMap<>();
        env.put(RelayMailConfig.ENV_HOST, "rig-mail-relay.rig-ron.svc.cluster.local");
        env.put(RelayMailConfig.ENV_PORT, "587");
        env.put(RelayMailConfig.ENV_USERNAME, "zad-keycloak");
        env.put(RelayMailConfig.ENV_PASSWORD, "geheim");
        env.put(RelayMailConfig.ENV_FROM, "noreply-inloggen@rijksoverheid.nl");
        return env;
    }

    /** De smtpServer van een realm die door een "aanvaller" is omgezet. */
    private static Map<String, String> realmSmtpVanDeAanvaller() {
        Map<String, String> config = new HashMap<>();
        config.put("host", "lokaas.rig-system.svc.cluster.local");
        config.put("port", "2525");
        config.put("auth", "true");
        config.put("user", "buit");
        config.put("password", "HET-GEHEIM-VAN-HET-PLATFORM");
        config.put("from", "van-de-aanvaller@example.org");
        config.put("starttls", "false");
        return config;
    }

    @Test
    public void deVerbindingKomtUitDeOmgevingEnNietUitDeRealm() {
        RelayEmailSenderProvider provider = new RelayEmailSenderProvider(RelayMailConfig.fromEnvironment(omgeving()));

        Properties props = provider.sessionProperties();

        assertEquals("rig-mail-relay.rig-ron.svc.cluster.local", props.getProperty("mail.smtp.host"));
        assertEquals("587", props.getProperty("mail.smtp.port"));
        assertEquals("noreply-inloggen@rijksoverheid.nl", props.getProperty("mail.smtp.from"));

        // Geen ONDERSCHEIDENDE waarde uit de smtpServer van de aanvaller mag hier
        // terugkomen. De booleans ("true"/"false") blijven buiten deze toets: die staan
        // in beide maps en zeggen niets over herkomst.
        Map<String, String> aanvaller = realmSmtpVanDeAanvaller();
        String alleWaarden = props.toString();
        for (String sleutel : new String[] {"host", "port", "user", "password", "from"}) {
            String waarde = aanvaller.get(sleutel);
            assertFalse("waarde uit de realm lekte in de sessie: " + waarde, alleWaarden.contains(waarde));
        }
    }

    @Test
    public void starttlsEnAuthStaanAan() {
        RelayEmailSenderProvider provider = new RelayEmailSenderProvider(RelayMailConfig.fromEnvironment(omgeving()));

        Properties props = provider.sessionProperties();

        assertEquals("true", props.getProperty("mail.smtp.auth"));
        assertEquals("true", props.getProperty("mail.smtp.starttls.enable"));
        assertEquals("true", props.getProperty("mail.smtp.starttls.required"));
    }

    /**
     * De proefversie had {@code ZAD_MAIL_RELAY_TRUST_ALL}, die {@code mail.smtp.ssl.trust=*}
     * zette en daarmee certificaat- EN hostnaamcontrole uitschakelde. Dat is in de meting
     * gemeld en hoort niet in productiecode: een schakelaar die aan een omgevingsvariabele
     * hangt reist mee naar een cluster waar TLS wel iets betekent.
     *
     * <p>Deze toets zet de oude variabele alsnog en eist dat er niets van overblijft.
     */
    @Test
    public void erIsGeenKnopMeerDieCertificaatcontroleUitzet() {
        Map<String, String> env = omgeving();
        env.put("ZAD_MAIL_RELAY_TRUST_ALL", "true");

        Properties props = new RelayEmailSenderProvider(RelayMailConfig.fromEnvironment(env)).sessionProperties();

        assertNull("mail.smtp.ssl.trust hoort niet meer gezet te worden", props.getProperty("mail.smtp.ssl.trust"));
        assertEquals("true", props.getProperty("mail.smtp.ssl.checkserveridentity"));
    }

    @Test
    public void deRealmKanStarttlsNietUitzetten() {
        Map<String, String> env = omgeving();
        // De aanvaller zet starttls uit in zijn realm; de omgeving zegt niets, dus de
        // standaard (aan) hoort te blijven staan.
        RelayEmailSenderProvider provider = new RelayEmailSenderProvider(RelayMailConfig.fromEnvironment(env));

        Properties props = provider.sessionProperties();

        assertEquals("true", props.getProperty("mail.smtp.starttls.required"));
        assertFalse(props.toString().contains("2525"));
    }

    /** Alleen de POD kan STARTTLS uitzetten, en alleen door het letterlijk op te schrijven. */
    @Test
    public void deOmgevingKanStarttlsUitzetten() {
        Map<String, String> env = omgeving();
        env.put(RelayMailConfig.ENV_STARTTLS, "false");

        Properties props = new RelayEmailSenderProvider(RelayMailConfig.fromEnvironment(env)).sessionProperties();

        assertEquals("false", props.getProperty("mail.smtp.starttls.enable"));
        assertEquals("false", props.getProperty("mail.smtp.starttls.required"));
        // Ook dan blijft AUTH aan staan: het account is wat de relay onderscheidt.
        assertEquals("true", props.getProperty("mail.smtp.auth"));
    }

    @Test
    public void eenOntbrekendeRelayIsEenHardeFoutEnGeenTerugval() {
        for (String sleutel : new String[] {
            RelayMailConfig.ENV_HOST,
            RelayMailConfig.ENV_USERNAME,
            RelayMailConfig.ENV_PASSWORD,
            RelayMailConfig.ENV_FROM
        }) {
            Map<String, String> env = omgeving();
            env.remove(sleutel);
            IllegalStateException fout = assertThrows(
                    sleutel + " ontbreekt maar werd geaccepteerd",
                    IllegalStateException.class,
                    () -> RelayMailConfig.fromEnvironment(env));
            assertTrue(fout.getMessage().contains(sleutel));
        }
    }

    @Test
    public void deStandaardpoortIs587() {
        Map<String, String> env = omgeving();
        env.remove(RelayMailConfig.ENV_PORT);

        assertEquals("587", RelayMailConfig.fromEnvironment(env).getPort());
    }

    @Test
    public void hetMerktekenBeweegtMeeMetDeVersie() {
        assertEquals("X-ZAD-Email-Sender", RelayMailConfig.MARKER_HEADER);
        assertEquals("zad-relay/" + RelayMailConfig.VERSION, RelayMailConfig.MARKER_VALUE);
        assertFalse("het merkteken noemt zichzelf geen proef meer", RelayMailConfig.MARKER_VALUE.contains("proef"));
    }
}
