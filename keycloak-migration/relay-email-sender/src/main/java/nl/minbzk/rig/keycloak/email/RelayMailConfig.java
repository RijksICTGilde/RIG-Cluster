package nl.minbzk.rig.keycloak.email;

import java.util.Map;

/**
 * De relay zoals de POD hem kent. Niets hiervan komt uit de realm.
 *
 * <p>Dat is de hele reden dat deze proef bestaat: zolang de bestemming uit de omgeving van
 * de pod komt, is er in geen enkele realm een veld dat een projectbeheerder kan omzetten
 * naar een luisteraar die hij zelf beheert.
 */
public final class RelayMailConfig {

    /** Het merkteken dat elk bericht meekrijgt, zodat "hij werkt" niet te verwarren is met "de standaardprovider deed het". */
    public static final String MARKER_HEADER = "X-ZAD-Email-Sender";

    /** De waarde van dat merkteken. Bewust met "proef" erin. */
    public static final String MARKER_VALUE = "zad-relay-proef/0.1.0";

    public static final String ENV_HOST = "ZAD_MAIL_RELAY_HOST";
    public static final String ENV_PORT = "ZAD_MAIL_RELAY_PORT";
    public static final String ENV_USERNAME = "ZAD_MAIL_RELAY_USERNAME";
    public static final String ENV_PASSWORD = "ZAD_MAIL_RELAY_PASSWORD";
    public static final String ENV_FROM = "ZAD_MAIL_RELAY_FROM";
    public static final String ENV_STARTTLS = "ZAD_MAIL_RELAY_STARTTLS";
    public static final String ENV_TRUST_ALL = "ZAD_MAIL_RELAY_TRUST_ALL";

    private final String host;
    private final String port;
    private final String username;
    private final String password;
    private final String from;
    private final boolean starttls;
    private final boolean trustAll;

    private RelayMailConfig(
            String host, String port, String username, String password, String from, boolean starttls, boolean trustAll) {
        this.host = host;
        this.port = port;
        this.username = username;
        this.password = password;
        this.from = from;
        this.starttls = starttls;
        this.trustAll = trustAll;
    }

    /**
     * Leest de relay uit de meegegeven omgeving.
     *
     * @param env de omgeving van de pod; als map meegegeven zodat een test hem kan zetten
     *            zonder de omgeving van de JVM te muteren.
     * @throws IllegalStateException als host, gebruiker, wachtwoord of afzender ontbreekt.
     *         Bewust hard: een halve configuratie die stil terugvalt op iets anders is
     *         precies de klasse fout die deze meting moet uitsluiten.
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
        return new RelayMailConfig(
                host, port, username, password, from, isTrue(env.get(ENV_STARTTLS), true), isTrue(env.get(ENV_TRUST_ALL), false));
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

    public boolean isTrustAll() {
        return trustAll;
    }
}
