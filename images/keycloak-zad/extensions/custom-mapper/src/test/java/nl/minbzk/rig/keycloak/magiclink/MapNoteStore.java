package nl.minbzk.rig.keycloak.magiclink;

import java.util.HashMap;
import java.util.Map;

/** A {@link NoteStore} in memory, standing in for the notes of an authentication session. */
class MapNoteStore implements NoteStore {

    private final Map<String, String> notes = new HashMap<>();

    @Override
    public String get(String name) {
        return notes.get(name);
    }

    @Override
    public void set(String name, String value) {
        notes.put(name, value);
    }

    @Override
    public void remove(String name) {
        notes.remove(name);
    }

    int size() {
        return notes.size();
    }
}
