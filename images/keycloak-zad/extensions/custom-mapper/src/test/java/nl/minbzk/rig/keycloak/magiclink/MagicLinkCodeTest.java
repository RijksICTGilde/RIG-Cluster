package nl.minbzk.rig.keycloak.magiclink;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import java.security.SecureRandom;
import org.junit.Test;

public class MagicLinkCodeTest {

    @Test
    public void generatesSixCharactersFromTheAlphabet() {
        SecureRandom random = new SecureRandom();
        for (int i = 0; i < 200; i++) {
            String code = MagicLinkCode.generate(random);
            assertEquals(MagicLinkCode.LENGTH, code.length());
            for (char c : code.toCharArray()) {
                assertTrue("unexpected character " + c, MagicLinkCode.ALPHABET.indexOf(c) >= 0);
            }
        }
    }

    @Test
    public void alphabetLeavesOutTheLookalikes() {
        for (char c : new char[] {'0', 'O', '1', 'I', 'L'}) {
            assertFalse("alphabet should not contain " + c, MagicLinkCode.ALPHABET.indexOf(c) >= 0);
        }
    }

    @Test
    public void normaliseUpperCasesAndDropsSpacesAndDashes() {
        assertEquals("ABC234", MagicLinkCode.normalise(" abc-234 "));
        assertEquals("ABC234", MagicLinkCode.normalise("AB C2 34"));
        assertEquals("ABC234", MagicLinkCode.normalise("abc234"));
        assertEquals("", MagicLinkCode.normalise(null));
    }

    @Test
    public void hashIsStableAndComparable() {
        String hash = MagicLinkCode.hash("ABC234");
        assertEquals(hash, MagicLinkCode.hash(MagicLinkCode.normalise("abc 234")));
        assertTrue(MagicLinkCode.hashMatches(hash, MagicLinkCode.hash("ABC234")));
        assertFalse(MagicLinkCode.hashMatches(hash, MagicLinkCode.hash("ABC235")));
        assertFalse(MagicLinkCode.hashMatches(null, hash));
    }
}
