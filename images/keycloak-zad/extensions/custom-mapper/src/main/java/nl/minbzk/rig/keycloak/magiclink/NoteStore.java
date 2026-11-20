package nl.minbzk.rig.keycloak.magiclink;

/**
 * The notes of one login attempt, as {@link MagicLinkState} reads and writes them.
 *
 * <p>An interface and not the authentication session itself, so the state machine of the
 * magic link is testable without a Keycloak session.
 */
public interface NoteStore {

    String get(String name);

    void set(String name, String value);

    void remove(String name);
}
