package nl.minbzk.rig.keycloak.magiclink;

import org.keycloak.sessions.AuthenticationSessionModel;

/** The authentication session of a login attempt, as a {@link NoteStore}. */
public final class AuthSessionNotes implements NoteStore {

    private final AuthenticationSessionModel authSession;

    public AuthSessionNotes(AuthenticationSessionModel authSession) {
        this.authSession = authSession;
    }

    @Override
    public String get(String name) {
        return authSession.getAuthNote(name);
    }

    @Override
    public void set(String name, String value) {
        authSession.setAuthNote(name, value);
    }

    @Override
    public void remove(String name) {
        authSession.removeAuthNote(name);
    }
}
