package nl.minbzk.rig.keycloak.magiclink;

/**
 * The outstanding code of one login attempt: issuing it, verifying it, and throwing it away.
 *
 * <p>Everything lives in the notes of the authentication session, so a code works only in the
 * login attempt that asked for it. Issuing a new code overwrites the previous one, and a
 * verified code is removed before the user is logged in, which makes both the code and the
 * link single-use.
 */
public final class MagicLinkState {

    public enum Verdict {
        OK,
        NO_CODE,
        EXPIRED,
        WRONG,
        /** Wrong, and the last attempt: the code is gone and the user has to ask for a new one. */
        WRONG_AND_SPENT
    }

    static final String NOTE_CODE_HASH = "zad-magic-link.code-hash";
    static final String NOTE_USER_ID = "zad-magic-link.user-id";
    static final String NOTE_EXPIRES_AT = "zad-magic-link.expires-at";
    static final String NOTE_ATTEMPTS_LEFT = "zad-magic-link.attempts-left";
    static final String NOTE_LINK_PRESENTED = "zad-magic-link.link-presented";

    public static final int MAX_ATTEMPTS = 2;

    public static final long VALID_FOR_MILLIS = 5 * 60 * 1000L;

    private MagicLinkState() {}

    public static void issue(NoteStore notes, String userId, String codeHash, long now) {
        notes.set(NOTE_USER_ID, userId);
        notes.set(NOTE_CODE_HASH, codeHash);
        notes.set(NOTE_EXPIRES_AT, Long.toString(now + VALID_FOR_MILLIS));
        notes.set(NOTE_ATTEMPTS_LEFT, Integer.toString(MAX_ATTEMPTS));
        notes.remove(NOTE_LINK_PRESENTED);
    }

    public static void clear(NoteStore notes) {
        notes.remove(NOTE_USER_ID);
        notes.remove(NOTE_CODE_HASH);
        notes.remove(NOTE_EXPIRES_AT);
        notes.remove(NOTE_ATTEMPTS_LEFT);
        notes.remove(NOTE_LINK_PRESENTED);
    }

    public static boolean hasOutstandingCode(NoteStore notes) {
        return notes.get(NOTE_CODE_HASH) != null;
    }

    public static String userId(NoteStore notes) {
        return notes.get(NOTE_USER_ID);
    }

    public static String codeHash(NoteStore notes) {
        return notes.get(NOTE_CODE_HASH);
    }

    /** Remember that a valid link was opened, so the flow shows the confirmation button. */
    public static void markLinkPresented(NoteStore notes) {
        notes.set(NOTE_LINK_PRESENTED, "true");
    }

    public static boolean linkPresented(NoteStore notes) {
        return "true".equals(notes.get(NOTE_LINK_PRESENTED));
    }

    /** Verify a typed code. On OK and on WRONG_AND_SPENT the state is gone afterwards. */
    public static Verdict verify(NoteStore notes, String typedCode, long now) {
        String storedHash = notes.get(NOTE_CODE_HASH);
        if (storedHash == null) {
            return Verdict.NO_CODE;
        }
        if (now > expiresAt(notes)) {
            clear(notes);
            return Verdict.EXPIRED;
        }
        if (MagicLinkCode.hashMatches(storedHash, MagicLinkCode.hash(MagicLinkCode.normalise(typedCode)))) {
            return Verdict.OK;
        }

        int attemptsLeft = attemptsLeft(notes) - 1;
        if (attemptsLeft <= 0) {
            clear(notes);
            return Verdict.WRONG_AND_SPENT;
        }
        notes.set(NOTE_ATTEMPTS_LEFT, Integer.toString(attemptsLeft));
        return Verdict.WRONG;
    }

    /** Whether the code this link carries is still the outstanding one and still valid. */
    public static boolean linkStillValid(NoteStore notes, String codeHashFromToken, long now) {
        return codeStillValid(notes, now) && MagicLinkCode.hashMatches(notes.get(NOTE_CODE_HASH), codeHashFromToken);
    }

    /** Whether there is an outstanding code that has not expired yet. */
    public static boolean codeStillValid(NoteStore notes, long now) {
        return notes.get(NOTE_CODE_HASH) != null && now <= expiresAt(notes);
    }

    public static long expiresAt(NoteStore notes) {
        return parseLong(notes.get(NOTE_EXPIRES_AT));
    }

    private static int attemptsLeft(NoteStore notes) {
        String value = notes.get(NOTE_ATTEMPTS_LEFT);
        if (value == null) {
            return MAX_ATTEMPTS;
        }
        try {
            return Integer.parseInt(value);
        } catch (NumberFormatException e) {
            return 0;
        }
    }

    private static long parseLong(String value) {
        if (value == null) {
            return 0L;
        }
        try {
            return Long.parseLong(value);
        } catch (NumberFormatException e) {
            return 0L;
        }
    }
}
