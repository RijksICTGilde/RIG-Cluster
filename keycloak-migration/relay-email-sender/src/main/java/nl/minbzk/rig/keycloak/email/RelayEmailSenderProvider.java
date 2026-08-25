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
 * PROEF (RC-158): verstuurt via de mailrelay van het platform en negeert {@code smtpServer}
 * van de realm volledig.
 *
 * <p>De bestemming, de inloggegevens en de afzender komen uit de OMGEVING VAN DE POD. De
 * {@code config}-map die Keycloak meegeeft is de {@code smtpServer} van de realm; die wordt
 * hier alleen GETELD en gelogd, nooit gebruikt. Zolang dat zo is, bestaat er in geen enkele
 * realm nog een veld waarmee een projectbeheerder de post naar een eigen luisteraar kan
 * sturen.
 *
 * <p>Elk bericht krijgt {@link RelayMailConfig#MARKER_HEADER} mee, zodat in de sink te zien
 * is dat dit bericht door DEZE code is verstuurd en niet door
 * {@code DefaultEmailSenderProvider}.
 *
 * <p>Deze klasse is een MEETOPSTELLING. Er zit geen herhaling, geen wachtrij en geen
 * foutafhandeling in die verder gaat dan de fout doorgeven.
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
        int genegeerdeSleutels = config == null ? 0 : config.size();
        String genegeerdeHost = config == null ? null : config.get("host");
        LOG.infof(
                "ZAD-RELAY-PROEF: bericht voor %s gaat naar relay %s:%s; smtpServer van de realm genegeerd (%d sleutels, host=%s)",
                address, relay.getHost(), relay.getPort(), genegeerdeSleutels, genegeerdeHost);

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
            LOG.errorf(e, "ZAD-RELAY-PROEF: versturen naar %s mislukt", address);
            throw new EmailException(e);
        }
        LOG.infof("ZAD-RELAY-PROEF: bericht voor %s aangeboden aan de relay", address);
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
     */
    Properties sessionProperties() {
        Properties props = new Properties();
        props.setProperty("mail.smtp.host", relay.getHost());
        props.setProperty("mail.smtp.port", relay.getPort());
        props.setProperty("mail.smtp.auth", "true");
        props.setProperty("mail.smtp.starttls.enable", String.valueOf(relay.isStarttls()));
        props.setProperty("mail.smtp.starttls.required", String.valueOf(relay.isStarttls()));
        props.setProperty("mail.smtp.connectiontimeout", "10000");
        props.setProperty("mail.smtp.timeout", "10000");
        props.setProperty("mail.smtp.writetimeout", "10000");
        props.setProperty("mail.smtp.from", relay.getFrom());
        if (relay.isTrustAll()) {
            props.setProperty("mail.smtp.ssl.trust", "*");
        }
        return props;
    }

    @Override
    public void close() {
        // Geen toestand om op te ruimen.
    }
}
