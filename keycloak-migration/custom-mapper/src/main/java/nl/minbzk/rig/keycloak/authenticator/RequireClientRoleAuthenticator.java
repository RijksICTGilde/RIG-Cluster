package nl.minbzk.rig.keycloak.authenticator;

import org.jboss.logging.Logger;
import org.keycloak.authentication.AuthenticationFlowContext;
import org.keycloak.authentication.AuthenticationFlowError;
import org.keycloak.authentication.Authenticator;
import org.keycloak.models.ClientModel;
import org.keycloak.models.KeycloakSession;
import org.keycloak.models.RealmModel;
import org.keycloak.models.RoleModel;
import org.keycloak.models.UserModel;
import org.keycloak.sessions.AuthenticationSessionModel;

/**
 * Authenticator that checks if a user has a required client role.
 *
 * This authenticator is designed for post-broker login flows where we need to
 * verify that a user has a specific client role before allowing access.
 *
 * Unlike conditional flows which fail when skipped, this authenticator
 * explicitly returns SUCCESS when the role is present, properly completing
 * the authentication flow.
 */
public class RequireClientRoleAuthenticator implements Authenticator {

    private static final Logger LOG = Logger.getLogger(RequireClientRoleAuthenticator.class);

    @Override
    public void authenticate(AuthenticationFlowContext context) {
        UserModel user = context.getUser();

        if (user == null) {
            LOG.warn("No user in authentication context");
            context.failure(AuthenticationFlowError.UNKNOWN_USER);
            return;
        }

        String clientId = getConfiguredClientId(context);
        String roleName = getConfiguredRoleName(context);
        String errorMessage = getConfiguredErrorMessage(context);

        LOG.debugf("Checking if user '%s' has role '%s' on client '%s'",
                   user.getUsername(), roleName, clientId);

        if (clientId == null || clientId.isEmpty()) {
            LOG.warn("Client ID not configured for RequireClientRoleAuthenticator");
            context.failure(AuthenticationFlowError.INTERNAL_ERROR);
            return;
        }

        if (roleName == null || roleName.isEmpty()) {
            LOG.warn("Role name not configured for RequireClientRoleAuthenticator");
            context.failure(AuthenticationFlowError.INTERNAL_ERROR);
            return;
        }

        if (userHasClientRole(context, clientId, roleName)) {
            LOG.debugf("User '%s' has required role '%s.%s' - allowing access",
                       user.getUsername(), clientId, roleName);
            context.success();
        } else {
            LOG.infof("User '%s' does not have required role '%s.%s' - denying access",
                      user.getUsername(), clientId, roleName);

            context.getEvent().error("access_denied_missing_role");
            context.failure(AuthenticationFlowError.ACCESS_DENIED,
                           context.form()
                                  .setError(errorMessage)
                                  .createErrorPage(jakarta.ws.rs.core.Response.Status.FORBIDDEN));
        }
    }

    @Override
    public void action(AuthenticationFlowContext context) {
        // No action needed - this authenticator doesn't have a form
    }

    @Override
    public boolean requiresUser() {
        return true;
    }

    @Override
    public boolean configuredFor(KeycloakSession session, RealmModel realm, UserModel user) {
        return true;
    }

    @Override
    public void setRequiredActions(KeycloakSession session, RealmModel realm, UserModel user) {
        // No required actions
    }

    @Override
    public void close() {
        // Nothing to close
    }

    private String getConfiguredClientId(AuthenticationFlowContext context) {
        String clientId = context.getAuthenticatorConfig() != null
            ? context.getAuthenticatorConfig().getConfig().get(RequireClientRoleAuthenticatorFactory.CONFIG_CLIENT_ID)
            : null;

        // If not configured, try to use the current client from the auth session
        if (clientId == null || clientId.isEmpty()) {
            AuthenticationSessionModel authSession = context.getAuthenticationSession();
            if (authSession != null && authSession.getClient() != null) {
                clientId = authSession.getClient().getClientId();
                LOG.debugf("Using current auth session client: %s", clientId);
            }
        }

        return clientId;
    }

    private String getConfiguredRoleName(AuthenticationFlowContext context) {
        return context.getAuthenticatorConfig() != null
            ? context.getAuthenticatorConfig().getConfig().get(RequireClientRoleAuthenticatorFactory.CONFIG_ROLE_NAME)
            : null;
    }

    private String getConfiguredErrorMessage(AuthenticationFlowContext context) {
        String errorMessage = context.getAuthenticatorConfig() != null
            ? context.getAuthenticatorConfig().getConfig().get(RequireClientRoleAuthenticatorFactory.CONFIG_ERROR_MESSAGE)
            : null;

        if (errorMessage == null || errorMessage.isEmpty()) {
            errorMessage = "${accessDeniedNoPermission}";
        }

        return errorMessage;
    }

    private boolean userHasClientRole(AuthenticationFlowContext context, String clientId, String roleName) {
        RealmModel realm = context.getRealm();
        UserModel user = context.getUser();

        ClientModel client = realm.getClientByClientId(clientId);
        if (client == null) {
            LOG.warnf("Client '%s' not found in realm '%s'", clientId, realm.getName());
            return false;
        }

        RoleModel role = client.getRole(roleName);
        if (role == null) {
            LOG.warnf("Role '%s' not found on client '%s'", roleName, clientId);
            return false;
        }

        return user.hasRole(role);
    }
}
