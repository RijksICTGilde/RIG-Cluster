package nl.minbzk.rig.keycloak.logout;

import jakarta.ws.rs.GET;
import jakarta.ws.rs.Path;
import jakarta.ws.rs.PathParam;
import jakarta.ws.rs.core.Response;
import jakarta.ws.rs.ext.Provider;
import java.net.URI;
import org.jboss.logging.Logger;
import org.keycloak.cookie.CookieProvider;
import org.keycloak.cookie.CookieType;
import org.keycloak.models.IdentityProviderModel;
import org.keycloak.models.KeycloakSession;
import org.keycloak.models.RealmModel;
import org.keycloak.models.UserSessionModel;
import org.keycloak.services.managers.AuthenticationManager;
import org.keycloak.services.resource.RealmResourceProvider;

/**
 * Broker-logout shim for federated IdPs whose logout endpoints are one-way
 * redirects to a logout page — no SAML {@code LogoutResponse}, no callback.
 *
 * <p>Keycloak's standard broker-logout flow parks the local user session while
 * waiting for the IdP's {@code LogoutResponse}; if it never arrives,
 * {@code finishBrowserLogout} never runs and the session lives until idle
 * timeout. This endpoint flips the order: kill the session first, then redirect
 * to the IdP's logout page, expecting no return trip.
 *
 * <p>Per-IdP wiring and operational details: see
 * {@code features/keycloak-always-clear-session-logout.md}.
 */
public class AlwaysClearSessionLogoutEndpoint implements RealmResourceProvider {

    public static final String ID = "always-clear-session-logout";
    static final String DOWNSTREAM_LOGOUT_CONFIG_KEY = "downstreamLogoutUrl";

    private final KeycloakSession session;

    public AlwaysClearSessionLogoutEndpoint(KeycloakSession session) {
        this.session = session;
    }

    @Override
    public Object getResource() {
        return new LogoutResource(session);
    }

    @Override
    public void close() {
    }

    /**
     * The {@code @Provider} annotation is mandatory: RESTEasy in Keycloak's
     * Quarkus distribution (v23+) silently skips JAX-RS annotations on resource
     * classes without it, leading to 404s with no diagnostic. See
     * <a href="https://github.com/keycloak/keycloak/issues/25882">keycloak#25882</a>.
     */
    @Provider
    public static class LogoutResource {
        private static final Logger logger = Logger.getLogger(LogoutResource.class);

        private final KeycloakSession session;

        public LogoutResource(KeycloakSession session) {
            this.session = session;
        }

        @GET
        @Path("{idpAlias}")
        public Response logout(@PathParam("idpAlias") String idpAlias) {
            RealmModel realm = session.getContext().getRealm();
            IdentityProviderModel idp = realm.getIdentityProviderByAlias(idpAlias);
            if (idp == null) {
                logger.warnf("IdP '%s' not found in realm '%s'", idpAlias, realm.getName());
                return Response.status(Response.Status.NOT_FOUND)
                    .entity("Identity provider not configured").build();
            }

            String downstreamUrl = idp.getConfig().get(DOWNSTREAM_LOGOUT_CONFIG_KEY);
            if (downstreamUrl == null || downstreamUrl.isBlank()) {
                logger.warnf("'%s' not set on IdP '%s'", DOWNSTREAM_LOGOUT_CONFIG_KEY, idpAlias);
                return Response.serverError()
                    .entity("Downstream logout URL not configured").build();
            }

            AuthenticationManager.AuthResult auth =
                AuthenticationManager.authenticateIdentityCookie(session, realm, true);

            if (auth != null && auth.getSession() != null) {
                UserSessionModel userSession = auth.getSession();
                logger.infof("Always-clear logout for user '%s' session '%s' via IdP '%s'",
                    userSession.getUser().getUsername(), userSession.getId(), idpAlias);

                // logoutBroker=false: we ARE the broker hop. Setting true would
                // make Keycloak walk the broker chain again, immediately redirecting
                // back to this endpoint — infinite recursion.
                AuthenticationManager.backchannelLogout(
                    session,
                    realm,
                    userSession,
                    session.getContext().getUri(),
                    session.getContext().getConnection(),
                    session.getContext().getRequestHeaders(),
                    false
                );

                // backchannelLogout kills the session record but does not write
                // Set-Cookie headers; we must expire cookies explicitly here so the
                // browser doesn't retain stale references.
                CookieProvider cookies = session.getProvider(CookieProvider.class);
                cookies.expire(CookieType.IDENTITY);
                cookies.expire(CookieType.SESSION);
                cookies.expire(CookieType.AUTH_SESSION_ID);
            } else {
                logger.infof("No active Keycloak session for IdP '%s'; redirecting to downstream logout anyway", idpAlias);
            }

            return Response.seeOther(URI.create(downstreamUrl)).build();
        }
    }
}
