package nl.minbzk.rig.keycloak.magiclink;

import jakarta.ws.rs.core.Response;
import org.jboss.logging.Logger;
import org.keycloak.authentication.AuthenticationProcessor;
import org.keycloak.authentication.actiontoken.AbstractActionTokenHandler;
import org.keycloak.authentication.actiontoken.ActionTokenContext;
import org.keycloak.common.util.Time;
import org.keycloak.events.EventType;
import org.keycloak.forms.login.LoginFormsProvider;
import org.keycloak.services.messages.Messages;
import org.keycloak.services.resources.LoginActionsService;
import org.keycloak.sessions.AuthenticationSessionModel;

/**
 * Handles the link from the magic-link mail.
 *
 * <p>Opening the link does not log anyone in: mail scanners open links beforehand, and a link
 * that logged in on a GET would be spent before the user ever saw the mail. This marks the
 * link as presented and hands the browser back to the realm's browser flow, where
 * {@link MagicLinkAuthenticator} shows the confirmation button and only its POST logs in.
 *
 * <p>The button cannot post back to this handler: {@code /login-actions/action-token} is
 * GET and HEAD only in Keycloak, so a POST there answers 405.
 *
 * <p>The token may be presented repeatedly ({@link #canUseTokenRepeatedly}), because the
 * single-use bookkeeping is the outstanding code in the authentication session and not
 * Keycloak's token store: the code and the link have to die together.
 */
public class MagicLinkActionTokenHandler extends AbstractActionTokenHandler<MagicLinkActionToken> {

    private static final Logger LOG = Logger.getLogger(MagicLinkActionTokenHandler.class);

    public MagicLinkActionTokenHandler() {
        super(
                MagicLinkActionToken.TOKEN_TYPE,
                MagicLinkActionToken.class,
                Messages.INVALID_CODE,
                EventType.CUSTOM_REQUIRED_ACTION,
                "invalid_code");
    }

    @Override
    public boolean canUseTokenRepeatedly(MagicLinkActionToken token, ActionTokenContext<MagicLinkActionToken> context) {
        return true;
    }

    @Override
    public Response handleToken(MagicLinkActionToken token, ActionTokenContext<MagicLinkActionToken> context) {
        AuthenticationSessionModel authSession = context.getAuthenticationSession();
        NoteStore notes = new AuthSessionNotes(authSession);

        if (!MagicLinkState.linkStillValid(notes, token.getCodeHash(), Time.currentTimeMillis())) {
            LOG.info("ZAD magic link: link presented for a code that is no longer outstanding");
            context.getEvent().detail("zad_magic_link", "link_expired").error("expired_code");
            return loginForm(context, authSession)
                    .setError(Messages.EXPIRED_ACTION)
                    .createErrorPage(Response.Status.BAD_REQUEST);
        }

        MagicLinkState.markLinkPresented(notes);
        return context.processFlow(
                false,
                LoginActionsService.AUTHENTICATE_PATH,
                context.getRealm().getBrowserFlow(),
                null,
                new AuthenticationProcessor());
    }

    private static LoginFormsProvider loginForm(
            ActionTokenContext<MagicLinkActionToken> context, AuthenticationSessionModel authSession) {
        return context.getSession().getProvider(LoginFormsProvider.class).setAuthenticationSession(authSession);
    }
}
