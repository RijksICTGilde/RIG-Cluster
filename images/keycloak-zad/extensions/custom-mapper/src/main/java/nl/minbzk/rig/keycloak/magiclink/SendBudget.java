package nl.minbzk.rig.keycloak.magiclink;

/**
 * How often one user may ask for a magic link, so the form cannot be used as a mail bomb.
 *
 * <p>Two limits at once: never twice within {@link #MIN_INTERVAL_MILLIS}, and never more than
 * {@link #MAX_PER_WINDOW} within {@link #WINDOW_MILLIS}. Stored as one user attribute, because
 * the limit has to hold across login attempts and an authentication session does not.
 */
public record SendBudget(long lastSentAt, long windowStart, int sentInWindow) {

    public static final long MIN_INTERVAL_MILLIS = 30 * 1000L;
    public static final long WINDOW_MILLIS = 15 * 60 * 1000L;
    public static final int MAX_PER_WINDOW = 5;

    public static final SendBudget EMPTY = new SendBudget(0L, 0L, 0);

    /** Parses what {@link #format()} wrote; anything unreadable counts as no history. */
    public static SendBudget parse(String stored) {
        if (stored == null || stored.isBlank()) {
            return EMPTY;
        }
        String[] parts = stored.split(":");
        if (parts.length != 3) {
            return EMPTY;
        }
        try {
            return new SendBudget(Long.parseLong(parts[0]), Long.parseLong(parts[1]), Integer.parseInt(parts[2]));
        } catch (NumberFormatException e) {
            return EMPTY;
        }
    }

    public String format() {
        return lastSentAt + ":" + windowStart + ":" + sentInWindow;
    }

    public boolean allows(long now) {
        if (now - lastSentAt < MIN_INTERVAL_MILLIS) {
            return false;
        }
        return windowHasRoom(now);
    }

    public SendBudget registerSend(long now) {
        if (windowExpired(now)) {
            return new SendBudget(now, now, 1);
        }
        return new SendBudget(now, windowStart, sentInWindow + 1);
    }

    private boolean windowHasRoom(long now) {
        return windowExpired(now) || sentInWindow < MAX_PER_WINDOW;
    }

    private boolean windowExpired(long now) {
        return now - windowStart >= WINDOW_MILLIS;
    }
}
