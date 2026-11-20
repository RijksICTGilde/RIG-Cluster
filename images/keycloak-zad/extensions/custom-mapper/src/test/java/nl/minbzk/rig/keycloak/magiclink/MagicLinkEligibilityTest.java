package nl.minbzk.rig.keycloak.magiclink;

import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertSame;
import static org.mockito.ArgumentMatchers.any;
import static org.mockito.ArgumentMatchers.eq;
import static org.mockito.Mockito.mock;
import static org.mockito.Mockito.when;

import org.junit.Before;
import org.junit.Test;
import org.keycloak.models.FederatedIdentityModel;
import org.keycloak.models.KeycloakSession;
import org.keycloak.models.RealmModel;
import org.keycloak.models.UserModel;
import org.keycloak.models.UserProvider;

public class MagicLinkEligibilityTest {

    private static final String EMAIL = "beheerder@rijksoverheid.nl";
    private static final String PROVIDER = "sso-rijk";

    private KeycloakSession session;
    private UserProvider users;
    private RealmModel realm;
    private UserModel user;

    @Before
    public void setUp() {
        session = mock(KeycloakSession.class);
        users = mock(UserProvider.class);
        realm = mock(RealmModel.class);
        user = mock(UserModel.class);
        when(session.users()).thenReturn(users);
        when(user.isEnabled()).thenReturn(true);
    }

    @Test
    public void aUserWithALinkedIdentityIsEligible() {
        when(users.getUserByEmail(realm, EMAIL)).thenReturn(user);
        when(users.getFederatedIdentity(realm, user, PROVIDER)).thenReturn(mock(FederatedIdentityModel.class));

        assertSame(user, MagicLinkAuthenticator.eligibleUser(session, realm, EMAIL, PROVIDER));
    }

    @Test
    public void aLocalAccountWithoutALinkedIdentityIsNot() {
        when(users.getUserByEmail(realm, EMAIL)).thenReturn(user);
        when(users.getFederatedIdentity(realm, user, PROVIDER)).thenReturn(null);

        assertNull(MagicLinkAuthenticator.eligibleUser(session, realm, EMAIL, PROVIDER));
    }

    @Test
    public void anUnknownAddressIsNot() {
        when(users.getUserByEmail(realm, EMAIL)).thenReturn(null);

        assertNull(MagicLinkAuthenticator.eligibleUser(session, realm, EMAIL, PROVIDER));
    }

    @Test
    public void aDisabledUserIsNot() {
        when(user.isEnabled()).thenReturn(false);
        when(users.getUserByEmail(realm, EMAIL)).thenReturn(user);

        assertNull(MagicLinkAuthenticator.eligibleUser(session, realm, EMAIL, PROVIDER));
    }

    @Test
    public void anotherProviderDoesNotCount() {
        when(users.getUserByEmail(realm, EMAIL)).thenReturn(user);
        when(users.getFederatedIdentity(eq(realm), eq(user), any())).thenReturn(null);
        when(users.getFederatedIdentity(realm, user, "ander-idp")).thenReturn(mock(FederatedIdentityModel.class));

        assertNull(MagicLinkAuthenticator.eligibleUser(session, realm, EMAIL, PROVIDER));
    }
}
