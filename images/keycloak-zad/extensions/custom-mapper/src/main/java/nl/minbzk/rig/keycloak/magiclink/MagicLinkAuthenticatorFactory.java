package nl.minbzk.rig.keycloak.magiclink;

import java.util.List;
import org.keycloak.Config;
import org.keycloak.authentication.Authenticator;
import org.keycloak.authentication.AuthenticatorFactory;
import org.keycloak.models.AuthenticationExecutionModel;
import org.keycloak.models.KeycloakSession;
import org.keycloak.models.KeycloakSessionFactory;
import org.keycloak.provider.ProviderConfigProperty;

/** Registers {@link MagicLinkAuthenticator} and the identity provider it demands a link with. */
public class MagicLinkAuthenticatorFactory implements AuthenticatorFactory {

    public static final String PROVIDER_ID = "zad-magic-link-authenticator";

    public static final String CONFIG_IDENTITY_PROVIDER = "identityProvider";

    public static final String DEFAULT_IDENTITY_PROVIDER = "sso-rijk";

    private static final List<ProviderConfigProperty> CONFIG_PROPERTIES;

    static {
        ProviderConfigProperty identityProvider = new ProviderConfigProperty();
        identityProvider.setName(CONFIG_IDENTITY_PROVIDER);
        identityProvider.setLabel("Identity provider alias");
        identityProvider.setHelpText("Only users with a linked identity of this provider can log in with a magic link. "
                + "A local account therefore cannot bypass its password and OTP this way.");
        identityProvider.setType(ProviderConfigProperty.STRING_TYPE);
        identityProvider.setDefaultValue(DEFAULT_IDENTITY_PROVIDER);

        CONFIG_PROPERTIES = List.of(identityProvider);
    }

    @Override
    public String getId() {
        return PROVIDER_ID;
    }

    @Override
    public String getDisplayType() {
        return "ZAD Magic Link (e-mail code)";
    }

    @Override
    public String getHelpText() {
        return "Logging in with a one-time code and link by mail, as a fallback while the identity provider is down.";
    }

    @Override
    public String getReferenceCategory() {
        return "magic-link";
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
            AuthenticationExecutionModel.Requirement.ALTERNATIVE,
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
        // Not a singleton: the authenticator carries a SecureRandom of its own.
        return new MagicLinkAuthenticator();
    }

    @Override
    public void init(Config.Scope config) {
        // No initialization needed.
    }

    @Override
    public void postInit(KeycloakSessionFactory factory) {
        // No post-initialization needed.
    }

    @Override
    public void close() {
        // Nothing to close.
    }
}
