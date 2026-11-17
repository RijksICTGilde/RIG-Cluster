package nl.minbzk.rig.keycloak.magiclink;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.security.SecureRandom;

/**
 * The one-time code of a magic link: how it is generated, how typed input is normalised, and
 * how it is stored.
 *
 * <p>The alphabet leaves out the characters that are read for one another in a mail client
 * (0/O, 1/I/L), because the code is meant to be typed over by hand.
 *
 * <p>Only the HASH of a code is ever stored, so neither a log line nor a database row can
 * hand out a working code.
 */
public final class MagicLinkCode {

    static final String ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ";

    public static final int LENGTH = 6;

    private MagicLinkCode() {}

    public static String generate(SecureRandom random) {
        StringBuilder code = new StringBuilder(LENGTH);
        for (int i = 0; i < LENGTH; i++) {
            code.append(ALPHABET.charAt(random.nextInt(ALPHABET.length())));
        }
        return code.toString();
    }

    /** Upper case, without the spaces and dashes someone may type or paste along. */
    public static String normalise(String typed) {
        if (typed == null) {
            return "";
        }
        StringBuilder normalised = new StringBuilder(typed.length());
        for (char c : typed.toCharArray()) {
            if (c != ' ' && c != '-' && !Character.isWhitespace(c)) {
                normalised.append(Character.toUpperCase(c));
            }
        }
        return normalised.toString();
    }

    public static String hash(String normalisedCode) {
        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            byte[] bytes = digest.digest(normalisedCode.getBytes(StandardCharsets.UTF_8));
            StringBuilder hex = new StringBuilder(bytes.length * 2);
            for (byte b : bytes) {
                hex.append(Character.forDigit((b >> 4) & 0xf, 16));
                hex.append(Character.forDigit(b & 0xf, 16));
            }
            return hex.toString();
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("SHA-256 is not available", e);
        }
    }

    /** Constant-time comparison, so a wrong code gives nothing away about the right one. */
    public static boolean hashMatches(String storedHash, String candidateHash) {
        if (storedHash == null || candidateHash == null) {
            return false;
        }
        return MessageDigest.isEqual(
                storedHash.getBytes(StandardCharsets.UTF_8), candidateHash.getBytes(StandardCharsets.UTF_8));
    }
}
