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

import java.util.Arrays;
import java.util.Collections;
import java.util.List;
import java.util.stream.Collectors;

/**
 * Authenticator that checks if a user has a required role (client or realm).
 *
 * This authenticator is designed for post-broker login flows where we need to
 * verify that a user has a specific role before allowing access.
 *
 * Role type is determined by the clientId configuration:
 * - If clientId is provided: checks for a client role on that client
 * - If clientId is empty/null: checks for a realm role
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

        // Check if the current OAuth client should skip this role check
        String currentOAuthClientId = getCurrentOAuthClientId(context);
        List<String> skipClients = getConfiguredSkipClients(context);

        if (currentOAuthClientId != null && skipClients.contains(currentOAuthClientId)) {
            LOG.infof("Skipping role check for user '%s' - OAuth client '%s' is in skip list",
                      user.getUsername(), currentOAuthClientId);
            context.success();
            return;
        }

        String clientId = getConfiguredClientId(context);
        String roleName = getConfiguredRoleName(context);
        String errorMessage = getConfiguredErrorMessage(context);

        // Determine if this is a realm role or client role check
        boolean isRealmRole = (clientId == null || clientId.isEmpty());
        String roleDescription = isRealmRole ? roleName : clientId + "." + roleName;

        LOG.debugf("Checking if user '%s' has %s role '%s' (OAuth client: %s)",
                   user.getUsername(), isRealmRole ? "realm" : "client", roleDescription, currentOAuthClientId);

        if (roleName == null || roleName.isEmpty()) {
            LOG.warn("Role name not configured for RequireClientRoleAuthenticator");
            context.failure(AuthenticationFlowError.INTERNAL_ERROR);
            return;
        }

        boolean hasRole = isRealmRole
            ? userHasRealmRole(context, roleName)
            : userHasClientRole(context, clientId, roleName);

        if (hasRole) {
            LOG.debugf("User '%s' has required %s role '%s' - allowing access",
                       user.getUsername(), isRealmRole ? "realm" : "client", roleDescription);
            context.success();
        } else {
            LOG.infof("User '%s' does not have required %s role '%s' - denying access",
                      user.getUsername(), isRealmRole ? "realm" : "client", roleDescription);

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
        // Return the configured client ID, or null/empty for realm roles
        // Note: We no longer fall back to current client - empty means realm role
        return context.getAuthenticatorConfig() != null
            ? context.getAuthenticatorConfig().getConfig().get(RequireClientRoleAuthenticatorFactory.CONFIG_CLIENT_ID)
            : null;
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

    private boolean userHasRealmRole(AuthenticationFlowContext context, String roleName) {
        RealmModel realm = context.getRealm();
        UserModel user = context.getUser();

        RoleModel role = realm.getRole(roleName);
        if (role == null) {
            LOG.warnf("Realm role '%s' not found in realm '%s'", roleName, realm.getName());
            return false;
        }

        return user.hasRole(role);
    }

    /**
     * Get the current OAuth client ID from the authentication session.
     * This is the client that initiated the OAuth flow (e.g., the invite client).
     */
    private String getCurrentOAuthClientId(AuthenticationFlowContext context) {
        AuthenticationSessionModel authSession = context.getAuthenticationSession();
        if (authSession != null && authSession.getClient() != null) {
            return authSession.getClient().getClientId();
        }
        return null;
    }

    /**
     * Get the list of client IDs that should skip this role check.
     */
    private List<String> getConfiguredSkipClients(AuthenticationFlowContext context) {
        String skipClientsConfig = context.getAuthenticatorConfig() != null
            ? context.getAuthenticatorConfig().getConfig().get(RequireClientRoleAuthenticatorFactory.CONFIG_SKIP_CLIENTS)
            : null;

        if (skipClientsConfig == null || skipClientsConfig.trim().isEmpty()) {
            return Collections.emptyList();
        }

        return Arrays.stream(skipClientsConfig.split(","))
                     .map(String::trim)
                     .filter(s -> !s.isEmpty())
                     .collect(Collectors.toList());
    }
}
