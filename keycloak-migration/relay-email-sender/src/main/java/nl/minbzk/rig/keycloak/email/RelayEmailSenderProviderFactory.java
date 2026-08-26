package nl.minbzk.rig.keycloak.email;

import org.jboss.logging.Logger;
import org.keycloak.Config;
import org.keycloak.email.EmailSenderProvider;
import org.keycloak.email.EmailSenderProviderFactory;
import org.keycloak.models.KeycloakSession;
import org.keycloak.models.KeycloakSessionFactory;

/**
 * Registreert {@link RelayEmailSenderProvider} onder {@link RelayMailConfig#PROVIDER_ID}.
 *
 * <p>De {@code emailSender}-SPI is SYSTEEMBREED: er is er precies EEN voor de hele server en
 * hij is niet per realm in te stellen. Dat is hier de bedoeling, geen beperking.
 *
 * <p>Het is bovendien een INTERNE SPI - Keycloak zegt dat zelf bij het opstarten met
 * {@code KC-SERVICES0047} - en die mag zonder aankondiging veranderen. Een major upgrade
 * vraagt dus een hertoets; zie {@code features/keycloak-26-upgrade.md}.
 *
 * <p>{@code order()} wordt bewust NIET overschreven. Zo kan alleen de expliciete
 * standaardprovider-vlag deze fabriek aanwijzen, en niet een volgorde die toevallig wint.
 */
public class RelayEmailSenderProviderFactory implements EmailSenderProviderFactory {

    public static final String PROVIDER_ID = RelayMailConfig.PROVIDER_ID;

    private static final Logger LOG = Logger.getLogger(RelayEmailSenderProviderFactory.class);

    private RelayMailConfig relay;
    private IllegalStateException configuratieFout;

    @Override
    public EmailSenderProvider create(KeycloakSession session) {
        if (relay == null) {
            throw configuratieFout != null
                    ? configuratieFout
                    : new IllegalStateException(
                            "ZAD-RELAY: geen relayconfiguratie in de omgeving van de pod; er wordt niets verstuurd");
        }
        return new RelayEmailSenderProvider(relay);
    }

    @Override
    public void init(Config.Scope scope) {
        try {
            relay = RelayMailConfig.fromEnvironment(System.getenv());
            LOG.infof(
                    "ZAD-RELAY %s: aangezet, relay %s:%s als %s, afzender %s",
                    RelayMailConfig.VERSION, relay.getHost(), relay.getPort(), relay.getUsername(), relay.getFrom());
        } catch (IllegalStateException e) {
            // GEEN harde fout bij het opstarten, en dat is een afweging en geen slordigheid.
            // Deze fabriek zit in het opstartpad van Keycloak zelf; hier gooien betekent dat
            // een ontbrekende mailvariabele het INLOGGEN van het hele platform plat legt.
            // De fout wordt bewaard en gegooid zodra er werkelijk iets verstuurd moet
            // worden: dan faalt de mail, luid en zichtbaar, en blijft de rest overeind.
            configuratieFout = e;
            LOG.errorf("ZAD-RELAY: geen bruikbare relayconfiguratie in de omgeving: %s", e.getMessage());
        }
    }

    @Override
    public void postInit(KeycloakSessionFactory factory) {
        // Niets.
    }

    @Override
    public void close() {
        // Niets.
    }

    @Override
    public String getId() {
        return PROVIDER_ID;
    }
}
