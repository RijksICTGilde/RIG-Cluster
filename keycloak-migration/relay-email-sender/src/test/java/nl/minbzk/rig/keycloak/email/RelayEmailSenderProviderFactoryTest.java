package nl.minbzk.rig.keycloak.email;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.util.stream.Collectors;
import org.junit.Test;

/** Toetst de registratie: zonder deze regel ziet Keycloak de fabriek nooit. */
public class RelayEmailSenderProviderFactoryTest {

    @Test
    public void deFabriekStaatInMetaInfServices() throws Exception {
        try (InputStream in = getClass()
                .getClassLoader()
                .getResourceAsStream("META-INF/services/org.keycloak.email.EmailSenderProviderFactory")) {
            assertNotNull("META-INF/services-bestand ontbreekt in de jar", in);
            String inhoud = new BufferedReader(new InputStreamReader(in, StandardCharsets.UTF_8))
                    .lines()
                    .collect(Collectors.joining("\n"));
            assertTrue(inhoud.contains(RelayEmailSenderProviderFactory.class.getName()));
        }
    }

    @Test
    public void deProviderIdMerktZichzelfAlsProef() {
        assertEquals("zad-relay-proef", new RelayEmailSenderProviderFactory().getId());
    }

    /**
     * De vlag die de provider aanwijst wordt gevormd uit de SPI-naam ({@code emailSender}) en
     * dit id. Loopt het id uit de pas met wat er in de README en de meting staat, dan wijst
     * de vlag naar niets en valt Keycloak STIL terug op de standaardprovider.
     */
    @Test
    public void deVlagvormVolgtUitDeSpiNaamEnHetId() {
        assertEquals(
                "--spi-email-sender-provider=zad-relay-proef",
                "--spi-email-sender-provider=" + new RelayEmailSenderProviderFactory().getId());
    }
}
