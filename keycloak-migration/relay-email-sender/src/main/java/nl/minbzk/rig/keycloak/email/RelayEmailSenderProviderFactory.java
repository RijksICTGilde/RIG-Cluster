package nl.minbzk.rig.keycloak.email;

import org.jboss.logging.Logger;
import org.keycloak.Config;
import org.keycloak.email.EmailSenderProvider;
import org.keycloak.email.EmailSenderProviderFactory;
import org.keycloak.models.KeycloakSession;
import org.keycloak.models.KeycloakSessionFactory;

/**
 * PROEF (RC-158). Registreert {@link RelayEmailSenderProvider} onder {@link #PROVIDER_ID}.
 *
 * <p>De {@code emailSender}-SPI is SYSTEEMBREED: er is er precies EEN voor de hele server en
 * hij is niet per realm in te stellen. Dat is hier de bedoeling, geen beperking.
 *
 * <p>{@code order()} wordt bewust NIET overschreven. Zo kan alleen een expliciete
 * standaardprovider-vlag deze fabriek aanwijzen, en meet de proef dus de vlag en niet een
 * volgorde die toevallig wint.
 */
public class RelayEmailSenderProviderFactory implements EmailSenderProviderFactory {

    public static final String PROVIDER_ID = "zad-relay-proef";

    private static final Logger LOG = Logger.getLogger(RelayEmailSenderProviderFactory.class);

    private RelayMailConfig relay;
    private IllegalStateException configuratieFout;

    @Override
    public EmailSenderProvider create(KeycloakSession session) {
        if (relay == null) {
            throw configuratieFout != null
                    ? configuratieFout
                    : new IllegalStateException("ZAD-RELAY-PROEF: geen relayconfiguratie");
        }
        return new RelayEmailSenderProvider(relay);
    }

    @Override
    public void init(Config.Scope scope) {
        try {
            relay = RelayMailConfig.fromEnvironment(System.getenv());
            LOG.infof(
                    "ZAD-RELAY-PROEF: aangezet, relay %s:%s als %s, afzender %s",
                    relay.getHost(), relay.getPort(), relay.getUsername(), relay.getFrom());
        } catch (IllegalStateException e) {
            // Bewust geen harde fout bij het opstarten: de meting moet ook de situatie
            // kunnen laten zien waarin de provider WEL is aangewezen maar GEEN relay kent.
            configuratieFout = e;
            LOG.errorf("ZAD-RELAY-PROEF: geen bruikbare relayconfiguratie in de omgeving: %s", e.getMessage());
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
