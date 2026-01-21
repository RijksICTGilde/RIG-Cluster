package nl.minbzk.rig.keycloak.authenticator;

import org.keycloak.Config;
import org.keycloak.authentication.Authenticator;
import org.keycloak.authentication.AuthenticatorFactory;
import org.keycloak.models.AuthenticationExecutionModel;
import org.keycloak.models.KeycloakSession;
import org.keycloak.models.KeycloakSessionFactory;
import org.keycloak.provider.ProviderConfigProperty;

import java.util.List;

/**
 * Factory for RequireClientRoleAuthenticator.
 *
 * This factory registers the authenticator with Keycloak and defines
 * its configuration properties.
 */
public class RequireClientRoleAuthenticatorFactory implements AuthenticatorFactory {

    public static final String PROVIDER_ID = "require-client-role-authenticator";

    public static final String CONFIG_CLIENT_ID = "clientId";
    public static final String CONFIG_ROLE_NAME = "roleName";
    public static final String CONFIG_ERROR_MESSAGE = "errorMessage";
    public static final String CONFIG_SKIP_CLIENTS = "skipClients";

    private static final RequireClientRoleAuthenticator SINGLETON = new RequireClientRoleAuthenticator();

    private static final List<ProviderConfigProperty> CONFIG_PROPERTIES;

    static {
        ProviderConfigProperty clientId = new ProviderConfigProperty();
        clientId.setName(CONFIG_CLIENT_ID);
        clientId.setLabel("Client ID");
        clientId.setHelpText("The client ID to check the role against. If empty, uses the current authentication client.");
        clientId.setType(ProviderConfigProperty.STRING_TYPE);

        ProviderConfigProperty roleName = new ProviderConfigProperty();
        roleName.setName(CONFIG_ROLE_NAME);
        roleName.setLabel("Role Name");
        roleName.setHelpText("The client role name that the user must have to be allowed access.");
        roleName.setType(ProviderConfigProperty.STRING_TYPE);

        ProviderConfigProperty errorMessage = new ProviderConfigProperty();
        errorMessage.setName(CONFIG_ERROR_MESSAGE);
        errorMessage.setLabel("Error Message");
        errorMessage.setHelpText("The error message to display when access is denied. Use ${messageKey} format for theme messages, e.g. ${accessDeniedNoPermission}");
        errorMessage.setType(ProviderConfigProperty.STRING_TYPE);
        errorMessage.setDefaultValue("${accessDeniedNoPermission}");

        ProviderConfigProperty skipClients = new ProviderConfigProperty();
        skipClients.setName(CONFIG_SKIP_CLIENTS);
        skipClients.setLabel("Skip Clients");
        skipClients.setHelpText("Comma-separated list of OAuth client IDs that should bypass this role check. " +
                "Use this to allow specific clients (e.g., invite flow) to authenticate without the required role.");
        skipClients.setType(ProviderConfigProperty.STRING_TYPE);

        CONFIG_PROPERTIES = List.of(clientId, roleName, errorMessage, skipClients);
    }

    @Override
    public String getId() {
        return PROVIDER_ID;
    }

    @Override
    public String getDisplayType() {
        return "Require Client Role";
    }

    @Override
    public String getHelpText() {
        return "Allows access only if the user has a specific client role. " +
               "Use this in post-broker login flows to restrict access based on roles.";
    }

    @Override
    public String getReferenceCategory() {
        return "authorization";
    }

    @Override
    public boolean isConfigurable() {
        return true;
    }

    @Override
    public boolean isUserSetupAllowed() {
        return false;
    }

    @Override
    public AuthenticationExecutionModel.Requirement[] getRequirementChoices() {
        return new AuthenticationExecutionModel.Requirement[] {
            AuthenticationExecutionModel.Requirement.REQUIRED,
            AuthenticationExecutionModel.Requirement.DISABLED
        };
    }

    @Override
    public List<ProviderConfigProperty> getConfigProperties() {
        return CONFIG_PROPERTIES;
    }

    @Override
    public Authenticator create(KeycloakSession session) {
        return SINGLETON;
    }

    @Override
    public void init(Config.Scope config) {
        // No initialization needed
    }

    @Override
    public void postInit(KeycloakSessionFactory factory) {
        // No post-initialization needed
    }

    @Override
    public void close() {
        // Nothing to close
    }
}
