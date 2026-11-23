package nl.minbzk.rig.keycloak.magiclink;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.RETURNS_SELF;
import static org.mockito.Mockito.doAnswer;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

import jakarta.ws.rs.core.MultivaluedHashMap;
import jakarta.ws.rs.core.MultivaluedMap;
import jakarta.ws.rs.core.Response;
import java.util.ArrayList;
import java.util.List;
import org.junit.Before;
import org.junit.Test;
import org.keycloak.authentication.AuthenticationFlowContext;
import org.keycloak.common.util.Time;
import org.keycloak.email.EmailException;
import org.keycloak.events.EventBuilder;
import org.keycloak.forms.login.LoginFormsProvider;
import org.keycloak.http.HttpRequest;
import org.keycloak.models.FederatedIdentityModel;
import org.keycloak.models.KeycloakSession;
import org.keycloak.models.RealmModel;
import org.keycloak.models.UserModel;
import org.keycloak.models.UserProvider;
import org.keycloak.sessions.AuthenticationSessionModel;

/**
 * An unknown address may not be distinguishable from a known one, not even over two POSTs.
 *
 * <p>Without a decoy code the second step gave the answer away: a known address lands on the
 * code screen with "this code is not correct", an unknown one back on the e-mail screen
 * without a message. The two branches that send no mail, a spent send budget and a failed
 * send, told the same tale on the second POST and cost one mail and a second.
 */
public class MagicLinkEnumerationTest {

    private static final String UNKNOWN_EMAIL = "niemand@rijksoverheid.nl";
    private static final String KNOWN_EMAIL = "beheerder@rijksoverheid.nl";

    private MagicLinkAuthenticator authenticator = new MagicLinkAuthenticator();
    private final MapNoteStore notes = new MapNoteStore();
    private final List<String> formsShown = new ArrayList<>();
    private final List<String> errorsShown = new ArrayList<>();

    private AuthenticationFlowContext context;
    private MultivaluedMap<String, String> formData;
    private UserProvider users;
    private RealmModel realm;
    private UserModel knownUser;

    @Before
    public void setUp() {
        formData = new MultivaluedHashMap<>();

        AuthenticationSessionModel authSession = mock(AuthenticationSessionModel.class);
        when(authSession.getAuthNote(anyString())).thenAnswer(call -> notes.get(call.getArgument(0)));
        doAnswer(call -> {
                    notes.set(call.getArgument(0), call.getArgument(1));
                    return null;
                })
                .when(authSession)
                .setAuthNote(anyString(), anyString());
        doAnswer(call -> {
                    notes.remove(call.getArgument(0));
                    return null;
                })
                .when(authSession)
                .removeAuthNote(anyString());

        LoginFormsProvider form = mock(LoginFormsProvider.class);
        when(form.setAttribute(anyString(), any())).thenReturn(form);
        when(form.setError(anyString())).thenAnswer(call -> {
            errorsShown.add(call.getArgument(0));
            return form;
        });
        when(form.createForm(anyString())).thenAnswer(call -> {
            formsShown.add(call.getArgument(0));
            return mock(Response.class);
        });

        users = mock(UserProvider.class);
        KeycloakSession session = mock(KeycloakSession.class);
        when(session.users()).thenReturn(users);

        realm = mock(RealmModel.class);
        knownUser = mock(UserModel.class);
        when(knownUser.getId()).thenReturn("known-user-id");
        when(knownUser.getUsername()).thenReturn("beheerder");
        when(knownUser.isEnabled()).thenReturn(true);

        HttpRequest request = mock(HttpRequest.class);
        when(request.getDecodedFormParameters()).thenReturn(formData);

        context = mock(AuthenticationFlowContext.class);
        when(context.getAuthenticationSession()).thenReturn(authSession);
        when(context.getHttpRequest()).thenReturn(request);
        when(context.getSession()).thenReturn(session);
        when(context.getRealm()).thenReturn(realm);
        when(context.form()).thenReturn(form);
        when(context.newEvent()).thenReturn(mock(EventBuilder.class, RETURNS_SELF));
    }

    /** Makes KNOWN_EMAIL a user of this realm with a linked identity of the provider. */
    private void withKnownEligibleUser(String sendBudget) {
        when(users.getUserByEmail(realm, KNOWN_EMAIL)).thenReturn(knownUser);
        when(users.getFederatedIdentity(realm, knownUser, MagicLinkAuthenticatorFactory.DEFAULT_IDENTITY_PROVIDER))
                .thenReturn(mock(FederatedIdentityModel.class));
        when(knownUser.getFirstAttribute(MagicLinkAuthenticator.USER_ATTRIBUTE_SEND_BUDGET))
                .thenReturn(sendBudget);
    }

    @Test
    public void anUnknownAddressLeavesAnOutstandingCodeBehind() {
        post(MagicLinkAuthenticator.FIELD_EMAIL, UNKNOWN_EMAIL);

        assertEquals(List.of(MagicLinkAuthenticator.FORM_CODE_STEP), formsShown);
        assertTrue(MagicLinkState.hasOutstandingCode(notes));
        assertEquals(MagicLinkAuthenticator.DECOY_USER_ID, MagicLinkState.userId(notes));
    }

    @Test
    public void aCodeOnAnUnknownAddressIsWrongAndThenSpent() {
        post(MagicLinkAuthenticator.FIELD_EMAIL, UNKNOWN_EMAIL);
        formsShown.clear();

        post(MagicLinkAuthenticator.FIELD_CODE, "ZZZZZZ");
        assertEquals(List.of(MagicLinkAuthenticator.FORM_CODE_STEP), formsShown);
        assertEquals(List.of("zadMagicLinkCodeWrong"), errorsShown);

        formsShown.clear();
        errorsShown.clear();
        post(MagicLinkAuthenticator.FIELD_CODE, "YYYYYY");
        assertEquals(List.of(MagicLinkAuthenticator.FORM_EMAIL_STEP), formsShown);
        assertEquals(List.of("zadMagicLinkCodeSpent"), errorsShown);
    }

    @Test
    public void aConfirmationWithoutALinkDoesNotLogAnyoneIn() {
        post(MagicLinkAuthenticator.FIELD_EMAIL, UNKNOWN_EMAIL);
        formsShown.clear();

        post(MagicLinkAuthenticator.FIELD_CONFIRM, "true");
        assertEquals(List.of(MagicLinkAuthenticator.FORM_EMAIL_STEP), formsShown);
        assertEquals(List.of("zadMagicLinkCodeExpired"), errorsShown);
        assertTrue(notes.size() == 0);
    }

    @Test
    public void aSpentSendBudgetShowsTheCodeScreenJustLikeAnUnknownAddress() {
        long now = Time.currentTimeMillis();
        withKnownEligibleUser(new SendBudget(now, now, 1).format());

        post(MagicLinkAuthenticator.FIELD_EMAIL, KNOWN_EMAIL);

        assertEquals(List.of(MagicLinkAuthenticator.FORM_CODE_STEP), formsShown);
        assertEquals(List.of(), errorsShown);
        assertTrue(MagicLinkState.hasOutstandingCode(notes));
    }

    @Test
    public void aFailedSendShowsTheCodeScreenJustLikeAnUnknownAddress() {
        authenticator = new MagicLinkAuthenticator() {
            @Override
            void sendMail(AuthenticationFlowContext ignored, UserModel user, String code, String codeHash)
                    throws EmailException {
                throw new EmailException("the relay is down");
            }
        };
        withKnownEligibleUser(null);

        post(MagicLinkAuthenticator.FIELD_EMAIL, KNOWN_EMAIL);

        assertEquals(List.of(MagicLinkAuthenticator.FORM_CODE_STEP), formsShown);
        assertEquals(List.of(), errorsShown);
        assertEquals(MagicLinkAuthenticator.DECOY_USER_ID, MagicLinkState.userId(notes));
    }

    @Test
    public void aFailedSendLeavesAnEarlierCodeOfThisAttemptAlone() {
        long now = Time.currentTimeMillis();
        MagicLinkState.issue(notes, "known-user-id", MagicLinkCode.hash("ABCDEF"), now);
        authenticator = new MagicLinkAuthenticator() {
            @Override
            void sendMail(AuthenticationFlowContext ignored, UserModel user, String code, String codeHash)
                    throws EmailException {
                throw new EmailException("the relay is down");
            }
        };
        withKnownEligibleUser(null);

        post(MagicLinkAuthenticator.FIELD_EMAIL, KNOWN_EMAIL);

        assertEquals(List.of(MagicLinkAuthenticator.FORM_CODE_STEP), formsShown);
        assertEquals(MagicLinkCode.hash("ABCDEF"), MagicLinkState.codeHash(notes));
    }

    private void post(String field, String value) {
        formData.clear();
        formData.putSingle(field, value);
        authenticator.action(context);
    }
}
