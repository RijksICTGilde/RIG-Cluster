package nl.minbzk.rig.keycloak.magiclink;

import org.keycloak.authentication.actiontoken.DefaultActionToken;

/**
 * The token behind the link in the mail.
 *
 * <p>It carries the hash of the code it was sent with, so the link dies the moment the code
 * does: a used code, a newly requested code and an expired code all leave the link pointing
 * at a hash that is no longer the outstanding one.
 */
public class MagicLinkActionToken extends DefaultActionToken {

    public static final String TOKEN_TYPE = "zad-magic-link";

    private static final String NOTE_CODE_HASH = "zad-ch";

    public MagicLinkActionToken(
            String userId,
            int absoluteExpirationInSecs,
            String compoundAuthenticationSessionId,
            String clientId,
            String codeHash) {
        super(userId, TOKEN_TYPE, absoluteExpirationInSecs, null, compoundAuthenticationSessionId);
        issuedFor(clientId);
        setNote(NOTE_CODE_HASH, codeHash);
    }

    private MagicLinkActionToken() {
        // For deserialization.
    }

    public String getCodeHash() {
        return getNote(NOTE_CODE_HASH);
    }
}
