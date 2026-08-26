package nl.minbzk.rig.keycloak.email;

import java.util.Map;

/**
 * De relay zoals de POD hem kent. Niets hiervan komt uit de realm.
 *
 * <p>Dat is de hele reden dat deze provider bestaat: zolang de bestemming uit de omgeving
 * van de pod komt, is er in geen enkele realm een veld dat een projectbeheerder kan
 * omzetten naar een luisteraar die hij zelf beheert. Gemeten in RC-158, zie
 * {@code docs/rc158-emailsender-spi-meting.md}.
 */
public final class RelayMailConfig {

    /**
     * De versie van deze provider. Staat gelijk aan {@code <version>} in {@code pom.xml} en
     * wordt daar door {@code RelayVersieTest} tegen gehouden: het merkteken hieronder is het
     * enige wat in een postbus laat zien WELKE code een bericht verstuurde, en een versie
     * die blijft staan terwijl de code verandert maakt dat merkteken waardeloos.
     */
    public static final String VERSION = "1.0.0";

    /** Het provider-id waaronder Keycloak deze verzender kent. Staat in de startvlag van de pod. */
    public static final String PROVIDER_ID = "zad-relay";

    /**
     * Het merkteken dat elk bericht meekrijgt, zodat "hij werkt" niet te verwarren is met
     * "de standaardprovider deed het". Kost niets en is in de sink of de postbus het enige
     * onderscheid tussen de twee.
     */
    public static final String MARKER_HEADER = "X-ZAD-Email-Sender";

    /** De waarde van dat merkteken; beweegt mee met {@link #VERSION}. */
    public static final String MARKER_VALUE = PROVIDER_ID + "/" + VERSION;

    public static final String ENV_HOST = "ZAD_MAIL_RELAY_HOST";
    public static final String ENV_PORT = "ZAD_MAIL_RELAY_PORT";
    public static final String ENV_USERNAME = "ZAD_MAIL_RELAY_USERNAME";
    public static final String ENV_PASSWORD = "ZAD_MAIL_RELAY_PASSWORD";
    public static final String ENV_FROM = "ZAD_MAIL_RELAY_FROM";
    public static final String ENV_STARTTLS = "ZAD_MAIL_RELAY_STARTTLS";

    private final String host;
    private final String port;
    private final String username;
    private final String password;
    private final String from;
    private final boolean starttls;

    private RelayMailConfig(String host, String port, String username, String password, String from, boolean starttls) {
        this.host = host;
        this.port = port;
        this.username = username;
        this.password = password;
        this.from = from;
        this.starttls = starttls;
    }

    /**
     * Leest de relay uit de meegegeven omgeving.
     *
     * @param env de omgeving van de pod; als map meegegeven zodat een test hem kan zetten
     *            zonder de omgeving van de JVM te muteren.
     * @throws IllegalStateException als host, gebruiker, wachtwoord of afzender ontbreekt.
     *         Bewust hard: een halve configuratie die stil terugvalt op iets anders is
     *         precies de klasse fout die deze provider moet uitsluiten.
     */
    public static RelayMailConfig fromEnvironment(Map<String, String> env) {
        String host = trimmedOrNull(env.get(ENV_HOST));
        String username = trimmedOrNull(env.get(ENV_USERNAME));
        String password = env.get(ENV_PASSWORD);
        String from = trimmedOrNull(env.get(ENV_FROM));

        if (host == null) {
            throw new IllegalStateException(ENV_HOST + " is niet gezet");
        }
        if (username == null) {
            throw new IllegalStateException(ENV_USERNAME + " is niet gezet");
        }
        if (password == null || password.isEmpty()) {
            throw new IllegalStateException(ENV_PASSWORD + " is niet gezet");
        }
        if (from == null) {
            throw new IllegalStateException(ENV_FROM + " is niet gezet");
        }

        String port = trimmedOrNull(env.get(ENV_PORT));
        if (port == null) {
            port = "587";
        }
        // STARTTLS staat AAN tenzij de omgeving hem uitzet. De veilige kant is de
        // standaard, zodat het uitzetten een zichtbare regel in een manifest is en geen
        // stilte. In de sandbox biedt de submission-listener van de relay geen STARTTLS
        // aan (RC-158, met EHLO nagemeten), dus daar staat die regel er; zie
        // features/keycloak-mail.md.
        return new RelayMailConfig(host, port, username, password, from, isTrue(env.get(ENV_STARTTLS), true));
    }

    private static String trimmedOrNull(String value) {
        if (value == null) {
            return null;
        }
        String trimmed = value.trim();
        return trimmed.isEmpty() ? null : trimmed;
    }

    private static boolean isTrue(String value, boolean fallback) {
        if (value == null || value.trim().isEmpty()) {
            return fallback;
        }
        return "true".equalsIgnoreCase(value.trim());
    }

    public String getHost() {
        return host;
    }

    public String getPort() {
        return port;
    }

    public String getUsername() {
        return username;
    }

    public String getPassword() {
        return password;
    }

    public String getFrom() {
        return from;
    }

    public boolean isStarttls() {
        return starttls;
    }
}
