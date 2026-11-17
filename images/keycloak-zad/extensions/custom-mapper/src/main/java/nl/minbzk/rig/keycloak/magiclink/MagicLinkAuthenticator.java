package nl.minbzk.rig.keycloak.magiclink;

import jakarta.ws.rs.core.MultivaluedMap;
import java.security.SecureRandom;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import org.jboss.logging.Logger;
import org.keycloak.authentication.AuthenticationFlowContext;
import org.keycloak.authentication.AuthenticationFlowError;
import org.keycloak.authentication.Authenticator;
import org.keycloak.common.util.Time;
import org.keycloak.email.EmailException;
import org.keycloak.email.EmailTemplateProvider;
import org.keycloak.events.EventType;
import org.keycloak.forms.login.LoginFormsProvider;
import org.keycloak.models.KeycloakSession;
import org.keycloak.models.RealmModel;
import org.keycloak.models.UserModel;
import org.keycloak.services.Urls;
import org.keycloak.sessions.AuthenticationSessionCompoundId;
import org.keycloak.sessions.AuthenticationSessionModel;

/**
 * Logging in with a code and a link by mail, for the hours in which SSO Rijk is down.
 *
 * <p>Only a user who already exists in this realm AND carries a linked identity of the
 * configured identity provider gets a mail. No account is ever created, and an unknown address
 * gets the same screens AND the same state as a known one, so neither the first nor the second
 * POST is a way to find out who has an account here.
 *
 * <p>What makes the code safe is in {@link MagicLinkState}: it lives in the notes of the
 * login attempt that asked for it, it is valid for five minutes, it survives two wrong
 * attempts, and it is gone the moment it is used. {@link SendBudget} limits how often one
 * user can ask for a mail.
 */
public class MagicLinkAuthenticator implements Authenticator {

    private static final Logger LOG = Logger.getLogger(MagicLinkAuthenticator.class);

    static final String FORM_EMAIL_STEP = "zad-magic-link.ftl";
    static final String FORM_CODE_STEP = "zad-magic-link-code.ftl";
    static final String FORM_CONFIRM_STEP = "zad-magic-link-confirm.ftl";

    static final String FIELD_EMAIL = "email";
    static final String FIELD_CODE = "code";
    static final String FIELD_RESEND = "resend";
    static final String FIELD_CONFIRM = "confirm";

    /** Stands in for the user behind a decoy code, which no one can ever enter. */
    static final String DECOY_USER_ID = "zad-magic-link-decoy";

    static final String USER_ATTRIBUTE_SEND_BUDGET = "zad-magic-link-send-budget";

    private static final String EVENT_DETAIL = "zad_magic_link";

    private final SecureRandom random = new SecureRandom();

    @Override
    public void authenticate(AuthenticationFlowContext context) {
        NoteStore notes = new AuthSessionNotes(context.getAuthenticationSession());

        if (MagicLinkState.linkPresented(notes)) {
            context.challenge(form(context).createForm(FORM_CONFIRM_STEP));
            return;
        }

        if (MagicLinkState.hasOutstandingCode(notes)) {
            context.challenge(form(context).createForm(FORM_CODE_STEP));
            return;
        }

        context.challenge(form(context).createForm(FORM_EMAIL_STEP));
    }

    @Override
    public void action(AuthenticationFlowContext context) {
        MultivaluedMap<String, String> formData = context.getHttpRequest().getDecodedFormParameters();
        NoteStore notes = new AuthSessionNotes(context.getAuthenticationSession());

        if (formData.containsKey(FIELD_RESEND)) {
            MagicLinkState.clear(notes);
            context.challenge(form(context).createForm(FORM_EMAIL_STEP));
            return;
        }

        if (formData.containsKey(FIELD_CONFIRM)) {
            confirmLink(context, notes);
            return;
        }

        if (formData.containsKey(FIELD_CODE)) {
            verifyCode(context, notes, formData.getFirst(FIELD_CODE));
            return;
        }

        requestCode(context, notes, formData.getFirst(FIELD_EMAIL));
    }

    private void requestCode(AuthenticationFlowContext context, NoteStore notes, String typedEmail) {
        String email = typedEmail == null ? "" : typedEmail.trim();
        if (email.isEmpty()) {
            context.challenge(form(context)
                    .setError("zadMagicLinkEmailMissing")
                    .createForm(FORM_EMAIL_STEP));
            return;
        }

        long now = Time.currentTimeMillis();
        UserModel user = eligibleUser(context, email);
        if (user == null) {
            codeStepWithoutMail(context, notes, email, now);
            return;
        }

        SendBudget budget = SendBudget.parse(user.getFirstAttribute(USER_ATTRIBUTE_SEND_BUDGET));
        if (!budget.allows(now)) {
            LOG.infof("ZAD magic link: send budget spent for user '%s'", user.getUsername());
            context.newEvent()
                    .event(EventType.CUSTOM_REQUIRED_ACTION)
                    .user(user)
                    .detail(EVENT_DETAIL, "rate_limited")
                    .error("rate_limit_exceeded");
            codeStepWithoutMail(context, notes, email, now);
            return;
        }

        String code = MagicLinkCode.generate(random);
        String codeHash = MagicLinkCode.hash(code);

        // The code is only issued once the mail is on its way: a failed send then leaves an
        // earlier, still valid code of this login attempt alone.
        try {
            sendMail(context, user, code, codeHash);
        } catch (EmailException e) {
            LOG.error("ZAD magic link: sending the mail failed", e);
            context.newEvent()
                    .event(EventType.CUSTOM_REQUIRED_ACTION)
                    .user(user)
                    .detail(EVENT_DETAIL, "send_failed")
                    .error("email_send_failed");
            codeStepWithoutMail(context, notes, email, now);
            return;
        }

        MagicLinkState.issue(notes, user.getId(), codeHash, now);
        user.setSingleAttribute(USER_ATTRIBUTE_SEND_BUDGET, budget.registerSend(now).format());
        context.newEvent()
                .event(EventType.CUSTOM_REQUIRED_ACTION)
                .user(user)
                .detail(EVENT_DETAIL, "requested")
                .success();

        challengeCodeStep(context, email);
    }

    /**
     * The code screen for a branch that sends no mail: an unknown address, a spent send budget
     * and a failed send all land here, so none of them is a way to find out who has an account.
     * A still valid code of this login attempt stays as it is, and otherwise a decoy code takes
     * its place, which nobody can ever enter.
     */
    private void codeStepWithoutMail(AuthenticationFlowContext context, NoteStore notes, String email, long now) {
        if (!MagicLinkState.codeStillValid(notes, now)) {
            MagicLinkState.issue(notes, DECOY_USER_ID, MagicLinkCode.hash(MagicLinkCode.generate(random)), now);
        }
        challengeCodeStep(context, email);
    }

    private void challengeCodeStep(AuthenticationFlowContext context, String email) {
        context.challenge(form(context)
                .setAttribute("zadMagicLinkEmail", email)
                .createForm(FORM_CODE_STEP));
    }

    private void verifyCode(AuthenticationFlowContext context, NoteStore notes, String typedCode) {
        MagicLinkState.Verdict verdict = MagicLinkState.verify(notes, typedCode, Time.currentTimeMillis());
        switch (verdict) {
            case OK -> completeLogin(context, notes);
            case WRONG -> context.failureChallenge(
                    AuthenticationFlowError.INVALID_CREDENTIALS,
                    form(context).setError("zadMagicLinkCodeWrong").createForm(FORM_CODE_STEP));
            case WRONG_AND_SPENT -> context.failureChallenge(
                    AuthenticationFlowError.INVALID_CREDENTIALS,
                    form(context).setError("zadMagicLinkCodeSpent").createForm(FORM_EMAIL_STEP));
            case EXPIRED -> context.failureChallenge(
                    AuthenticationFlowError.EXPIRED_CODE,
                    form(context).setError("zadMagicLinkCodeExpired").createForm(FORM_EMAIL_STEP));
            case NO_CODE -> context.challenge(form(context).createForm(FORM_EMAIL_STEP));
        }
    }

    /** The POST behind the button on the page the link opens. */
    private void confirmLink(AuthenticationFlowContext context, NoteStore notes) {
        if (!MagicLinkState.linkPresented(notes) || !MagicLinkState.codeStillValid(notes, Time.currentTimeMillis())) {
            MagicLinkState.clear(notes);
            context.failureChallenge(
                    AuthenticationFlowError.EXPIRED_CODE,
                    form(context).setError("zadMagicLinkCodeExpired").createForm(FORM_EMAIL_STEP));
            return;
        }
        completeLogin(context, notes);
    }

    private void completeLogin(AuthenticationFlowContext context, NoteStore notes) {
        String userId = MagicLinkState.userId(notes);
        UserModel user = userId == null ? null : context.getSession().users().getUserById(context.getRealm(), userId);
        MagicLinkState.clear(notes);

        if (user == null || !user.isEnabled()) {
            LOG.warn("ZAD magic link: the user behind a verified code is gone or disabled");
            context.failure(AuthenticationFlowError.INVALID_USER);
            return;
        }

        context.newEvent()
                .event(EventType.CUSTOM_REQUIRED_ACTION)
                .user(user)
                .detail(EVENT_DETAIL, "used")
                .success();
        context.setUser(user);
        context.success();
    }

    private UserModel eligibleUser(AuthenticationFlowContext context, String email) {
        return eligibleUser(context.getSession(), context.getRealm(), email, requiredIdentityProvider(context));
    }

    /** The user behind this address, but only with a linked identity of ``provider``. */
    static UserModel eligibleUser(KeycloakSession session, RealmModel realm, String email, String provider) {
        UserModel user = session.users().getUserByEmail(realm, email);
        if (user == null || !user.isEnabled()) {
            return null;
        }
        if (session.users().getFederatedIdentity(realm, user, provider) == null) {
            LOG.infof(
                    "ZAD magic link: user '%s' has no linked '%s' identity, so no mail goes out",
                    user.getUsername(), provider);
            return null;
        }
        return user;
    }

    void sendMail(AuthenticationFlowContext context, UserModel user, String code, String codeHash)
            throws EmailException {
        AuthenticationSessionModel authSession = context.getAuthenticationSession();
        String clientId = authSession.getClient().getClientId();
        int expiresAt = Time.currentTime() + (int) (MagicLinkState.VALID_FOR_MILLIS / 1000L);

        MagicLinkActionToken token = new MagicLinkActionToken(
                user.getId(),
                expiresAt,
                AuthenticationSessionCompoundId.fromAuthSession(authSession).getEncodedId(),
                clientId,
                codeHash);
        String serialized = token.serialize(context.getSession(), context.getRealm(), context.getUriInfo());
        String link = Urls.actionTokenBuilder(
                        context.getUriInfo().getBaseUri(), serialized, clientId, authSession.getTabId(), null)
                .build(context.getRealm().getName())
                .toString();

        Map<String, Object> attributes = new HashMap<>();
        attributes.put("code", code);
        attributes.put("link", link);
        attributes.put("validMinutes", MagicLinkState.VALID_FOR_MILLIS / 60000L);

        context.getSession()
                .getProvider(EmailTemplateProvider.class)
                .setRealm(context.getRealm())
                .setUser(user)
                .setAuthenticationSession(authSession)
                .send("zadMagicLinkSubject", List.of(realmLabel(context.getRealm())), "zad-magic-link.ftl", attributes);
    }

    private static String realmLabel(RealmModel realm) {
        String displayName = realm.getDisplayName();
        return displayName == null || displayName.isBlank() ? realm.getName() : displayName;
    }

    private String requiredIdentityProvider(AuthenticationFlowContext context) {
        if (context.getAuthenticatorConfig() == null) {
            return MagicLinkAuthenticatorFactory.DEFAULT_IDENTITY_PROVIDER;
        }
        String configured = context.getAuthenticatorConfig()
                .getConfig()
                .get(MagicLinkAuthenticatorFactory.CONFIG_IDENTITY_PROVIDER);
        return configured == null || configured.isBlank()
                ? MagicLinkAuthenticatorFactory.DEFAULT_IDENTITY_PROVIDER
                : configured;
    }

    private LoginFormsProvider form(AuthenticationFlowContext context) {
        return context.form()
                .setAttribute("zadMagicLinkCodeLength", MagicLinkCode.LENGTH)
                .setAttribute("zadMagicLinkValidMinutes", MagicLinkState.VALID_FOR_MILLIS / 60000L);
    }

    @Override
    public boolean requiresUser() {
        return false;
    }

    @Override
    public boolean configuredFor(KeycloakSession session, RealmModel realm, UserModel user) {
        return true;
    }

    @Override
    public void setRequiredActions(KeycloakSession session, RealmModel realm, UserModel user) {
        // No required actions.
    }

    @Override
    public void close() {
        // Nothing to close.
    }
}
