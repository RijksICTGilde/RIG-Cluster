package nl.minbzk.rig.keycloak.email;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNotNull;
import static org.junit.Assert.assertTrue;

import java.io.BufferedReader;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.util.Locale;
import java.util.stream.Collectors;
import org.junit.Test;
import org.keycloak.email.EmailSenderSpi;

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
    public void hetProviderIdIsDeEneNaamDieOokInDeVlagStaat() {
        assertEquals("zad-relay", new RelayEmailSenderProviderFactory().getId());
        assertEquals(RelayMailConfig.PROVIDER_ID, new RelayEmailSenderProviderFactory().getId());
    }

    /**
     * De vlag die de provider aanwijst wordt gevormd uit de SPI-NAAM en dit id. Beide helften
     * worden hier tegen de bron getoetst, want de meting van RC-158 liet zien dat een
     * verkeerde vorm STIL wordt genegeerd: Keycloak start gewoon door en de standaardprovider
     * verstuurt. Er is dus geen signaal bij het opstarten dat dit voor ons kan opvangen.
     *
     * <p>De SPI-naam komt uit Keycloak zelf ({@code EmailSenderSpi.getName()}), niet uit een
     * string hier: verandert hij bij een upgrade, dan valt deze toets om in plaats van de
     * bevestigingsmail.
     */
    @Test
    public void deVlagvormVolgtUitDeSpiNaamEnHetId() {
        String spiNaam = new EmailSenderSpi().getName();
        assertEquals("emailSender", spiNaam);

        String vlag = "--spi-" + naarStreepjes(spiNaam) + "-provider=" + new RelayEmailSenderProviderFactory().getId();

        assertEquals("--spi-email-sender-provider=zad-relay", vlag);
        assertEquals(
                "KC_SPI_EMAIL_SENDER_PROVIDER",
                "KC_SPI_" + naarStreepjes(spiNaam).replace('-', '_').toUpperCase(Locale.ROOT) + "_PROVIDER");
    }

    /** camelCase naar de vorm die de Keycloak-CLI leest: {@code emailSender} -> {@code email-sender}. */
    private static String naarStreepjes(String camelCase) {
        return camelCase.replaceAll("([a-z0-9])([A-Z])", "$1-$2").toLowerCase(Locale.ROOT);
    }
}
