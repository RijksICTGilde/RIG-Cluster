package nl.minbzk.rig.keycloak.email;

import jakarta.mail.Message;
import jakarta.mail.MessagingException;
import jakarta.mail.Session;
import jakarta.mail.Transport;
import jakarta.mail.internet.InternetAddress;
import jakarta.mail.internet.MimeBodyPart;
import jakarta.mail.internet.MimeMessage;
import jakarta.mail.internet.MimeMultipart;
import java.io.UnsupportedEncodingException;
import java.util.Map;
import java.util.Properties;
import org.jboss.logging.Logger;
import org.keycloak.email.EmailException;
import org.keycloak.email.EmailSenderProvider;

/**
 * Verstuurt via de mailrelay van het platform en negeert {@code smtpServer} van de realm
 * volledig.
 *
 * <p>De bestemming, de inloggegevens en de afzender komen uit de OMGEVING VAN DE POD. De
 * {@code config}-map die Keycloak meegeeft is de {@code smtpServer} van de realm; die wordt
 * hier alleen GETELD, nooit gebruikt. Zolang dat zo is, bestaat er in geen enkele realm nog
 * een veld waarmee een projectbeheerder de post naar een eigen luisteraar kan sturen. De
 * meting waaruit deze opzet volgt staat in {@code docs/rc158-emailsender-spi-meting.md}.
 *
 * <p>Elk bericht krijgt {@link RelayMailConfig#MARKER_HEADER} mee, zodat in de sink of de
 * postbus te zien is dat dit bericht door DEZE code is verstuurd en niet door
 * {@code DefaultEmailSenderProvider}.
 *
 * <p>Bewust zonder herhaling, wachtrij of eigen foutafhandeling: Keycloak meldt een
 * mislukte verzending zelf als {@code SEND_VERIFY_EMAIL_ERROR} en de relay heeft een eigen
 * wachtrij. Een tweede wachtrij hier zou alleen de plek zijn waar post onzichtbaar blijft
 * hangen.
 */
public class RelayEmailSenderProvider implements EmailSenderProvider {

    private static final Logger LOG = Logger.getLogger(RelayEmailSenderProvider.class);

    private final RelayMailConfig relay;

    public RelayEmailSenderProvider(RelayMailConfig relay) {
        this.relay = relay;
    }

    @Override
    public void send(Map<String, String> config, String address, String subject, String textBody, String htmlBody)
            throws EmailException {
        // WAT HIER NIET IN DE LOG KOMT: geen enkele WAARDE uit `config`. Die map is de
        // smtpServer van de realm en een projectbeheerder schrijft hem, dus alles erin is
        // tenantinvoer - een host met nieuwe regels erin schrijft dan zijn eigen logregels.
        // Het AANTAL sleutels en of er een host in stond zijn afgeleide feiten die niet
        // door de tenant gevormd worden, en ze zeggen precies genoeg: ze laten zien dat
        // deze provider de realm zag en hem passeerde.
        int genegeerdeSleutels = config == null ? 0 : config.size();
        boolean realmNoemdeEenHost = config != null && config.get("host") != null;
        LOG.infof(
                "ZAD-RELAY: bericht gaat naar de relay %s:%s; smtpServer van de realm genegeerd (%d sleutels, eigen host: %s)",
                relay.getHost(), relay.getPort(), genegeerdeSleutels, realmNoemdeEenHost);

        try {
            Transport transport = null;
            try {
                Session session = Session.getInstance(sessionProperties());
                MimeMessage bericht = new MimeMessage(session);
                bericht.setHeader(RelayMailConfig.MARKER_HEADER, RelayMailConfig.MARKER_VALUE);
                bericht.setFrom(new InternetAddress(relay.getFrom()));
                bericht.setRecipients(Message.RecipientType.TO, InternetAddress.parse(address));
                bericht.setSubject(subject, "utf-8");
                vulLichaam(bericht, textBody, htmlBody);
                bericht.saveChanges();

                transport = session.getTransport("smtp");
                transport.connect(relay.getUsername(), relay.getPassword());
                transport.sendMessage(bericht, new InternetAddress[] {new InternetAddress(address)});
            } finally {
                if (transport != null) {
                    transport.close();
                }
            }
        } catch (MessagingException | UnsupportedEncodingException e) {
            // Het ontvangeradres staat hier bewust niet in: het komt van de gebruiker en de
            // logregel van Keycloak eromheen noemt de gebeurtenis toch al.
            LOG.error("ZAD-RELAY: versturen via de relay mislukt", e);
            throw new EmailException(e);
        }
        LOG.info("ZAD-RELAY: bericht aangeboden aan de relay");
    }

    private static void vulLichaam(MimeMessage bericht, String textBody, String htmlBody)
            throws MessagingException, UnsupportedEncodingException {
        if (textBody != null && htmlBody != null) {
            MimeMultipart multipart = new MimeMultipart("alternative");
            MimeBodyPart tekst = new MimeBodyPart();
            tekst.setText(textBody, "UTF-8");
            MimeBodyPart html = new MimeBodyPart();
            html.setContent(htmlBody, "text/html; charset=UTF-8");
            multipart.addBodyPart(tekst);
            multipart.addBodyPart(html);
            bericht.setContent(multipart);
        } else if (htmlBody != null) {
            bericht.setContent(htmlBody, "text/html; charset=UTF-8");
        } else {
            bericht.setText(textBody == null ? "" : textBody, "UTF-8");
        }
    }

    /**
     * De verbindingsinstellingen, uitsluitend uit {@link RelayMailConfig}.
     *
     * <p>Zichtbaar voor de test: dit is de plek waar een sleutel uit de realm binnen zou
     * kunnen sluipen, dus de test leest deze map en toetst dat de host van de realm er niet
     * in staat.
     *
     * <p>Er staat GEEN {@code mail.smtp.ssl.trust} in. De proefversie had een schakelaar die
     * elk certificaat accepteerde omdat de sink in de sandbox een zelfondertekend
     * certificaat draagt; die hoort niet in productiecode, want hij reist mee naar een
     * cluster waar TLS wel iets betekent. Staat er ooit TLS op de submission-listener, dan
     * staat verificatie dus meteen aan.
     */
    Properties sessionProperties() {
        Properties props = new Properties();
        props.setProperty("mail.smtp.host", relay.getHost());
        props.setProperty("mail.smtp.port", relay.getPort());
        props.setProperty("mail.smtp.auth", "true");
        props.setProperty("mail.smtp.starttls.enable", String.valueOf(relay.isStarttls()));
        props.setProperty("mail.smtp.starttls.required", String.valueOf(relay.isStarttls()));
        // Expliciet, ook al is dit de standaard van Jakarta Mail 2: hostnaamverificatie
        // uitzetten maakt STARTTLS betekenisloos, en een standaard die je niet opschrijft
        // is een standaard die bij de volgende bibliotheekversie stil kan omgaan.
        props.setProperty("mail.smtp.ssl.checkserveridentity", "true");
        props.setProperty("mail.smtp.connectiontimeout", "10000");
        props.setProperty("mail.smtp.timeout", "10000");
        props.setProperty("mail.smtp.writetimeout", "10000");
        props.setProperty("mail.smtp.from", relay.getFrom());
        return props;
    }

    @Override
    public void close() {
        // Geen toestand om op te ruimen.
    }
}
