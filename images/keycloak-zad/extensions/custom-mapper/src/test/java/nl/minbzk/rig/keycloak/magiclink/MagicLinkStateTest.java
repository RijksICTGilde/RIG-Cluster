package nl.minbzk.rig.keycloak.magiclink;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Before;
import org.junit.Test;

public class MagicLinkStateTest {

    private static final long NOW = 1_700_000_000_000L;
    private static final String CODE = "ABC234";
    private static final String USER_ID = "user-1";

    private MapNoteStore notes;

    @Before
    public void setUp() {
        notes = new MapNoteStore();
        MagicLinkState.issue(notes, USER_ID, MagicLinkCode.hash(CODE), NOW);
    }

    @Test
    public void theRightCodeIsAccepted() {
        assertEquals(MagicLinkState.Verdict.OK, MagicLinkState.verify(notes, CODE, NOW));
        assertEquals(USER_ID, MagicLinkState.userId(notes));
    }

    @Test
    public void theCodeIsCaseAndSpacingInsensitive() {
        assertEquals(MagicLinkState.Verdict.OK, MagicLinkState.verify(notes, " abc-2 34 ", NOW));
    }

    @Test
    public void aCodeOlderThanFiveMinutesIsRefusedAndGone() {
        assertEquals(
                MagicLinkState.Verdict.EXPIRED,
                MagicLinkState.verify(notes, CODE, NOW + MagicLinkState.VALID_FOR_MILLIS + 1));
        assertFalse(MagicLinkState.hasOutstandingCode(notes));
    }

    @Test
    public void justInsideFiveMinutesStillWorks() {
        assertEquals(
                MagicLinkState.Verdict.OK, MagicLinkState.verify(notes, CODE, NOW + MagicLinkState.VALID_FOR_MILLIS));
    }

    @Test
    public void twoWrongAttemptsSpendTheCode() {
        assertEquals(MagicLinkState.Verdict.WRONG, MagicLinkState.verify(notes, "ZZZZZZ", NOW));
        assertTrue(MagicLinkState.hasOutstandingCode(notes));
        assertEquals(MagicLinkState.Verdict.WRONG_AND_SPENT, MagicLinkState.verify(notes, "ZZZZZZ", NOW));
        assertFalse(MagicLinkState.hasOutstandingCode(notes));
        assertEquals(MagicLinkState.Verdict.NO_CODE, MagicLinkState.verify(notes, CODE, NOW));
    }

    @Test
    public void aNewCodeInvalidatesThePreviousOne() {
        MagicLinkState.issue(notes, USER_ID, MagicLinkCode.hash("XYZ345"), NOW);
        assertEquals(MagicLinkState.Verdict.WRONG, MagicLinkState.verify(notes, CODE, NOW));
        assertEquals(MagicLinkState.Verdict.OK, MagicLinkState.verify(notes, "XYZ345", NOW));
    }

    @Test
    public void aNewCodeRestoresTheAttemptBudget() {
        assertEquals(MagicLinkState.Verdict.WRONG, MagicLinkState.verify(notes, "ZZZZZZ", NOW));
        MagicLinkState.issue(notes, USER_ID, MagicLinkCode.hash("XYZ345"), NOW);
        assertEquals(MagicLinkState.Verdict.WRONG, MagicLinkState.verify(notes, "ZZZZZZ", NOW));
        assertEquals(MagicLinkState.Verdict.WRONG_AND_SPENT, MagicLinkState.verify(notes, "ZZZZZZ", NOW));
    }

    @Test
    public void aCodeFromAnotherLoginAttemptIsUnknownHere() {
        MapNoteStore other = new MapNoteStore();
        assertEquals(MagicLinkState.Verdict.NO_CODE, MagicLinkState.verify(other, CODE, NOW));
    }

    @Test
    public void clearingLeavesNothingBehind() {
        MagicLinkState.markLinkPresented(notes);
        MagicLinkState.clear(notes);
        assertEquals(0, notes.size());
        assertFalse(MagicLinkState.linkPresented(notes));
    }

    @Test
    public void issuingANewCodeTakesTheConfirmationButtonAway() {
        MagicLinkState.markLinkPresented(notes);
        MagicLinkState.issue(notes, "user-1", MagicLinkCode.hash("ABC234"), NOW);
        assertFalse(MagicLinkState.linkPresented(notes));
    }

    @Test
    public void theConfirmationOutlivesTheGetOnlyWhileTheCodeDoes() {
        assertTrue(MagicLinkState.codeStillValid(notes, NOW));
        assertFalse(MagicLinkState.codeStillValid(notes, NOW + MagicLinkState.VALID_FOR_MILLIS + 1));

        MagicLinkState.clear(notes);
        assertFalse(MagicLinkState.codeStillValid(notes, NOW));
    }

    @Test
    public void theLinkIsValidForTheOutstandingCodeOnly() {
        String hash = MagicLinkCode.hash(CODE);
        assertTrue(MagicLinkState.linkStillValid(notes, hash, NOW));
        assertFalse(MagicLinkState.linkStillValid(notes, MagicLinkCode.hash("XYZ345"), NOW));
        assertFalse(MagicLinkState.linkStillValid(notes, hash, NOW + MagicLinkState.VALID_FOR_MILLIS + 1));

        MagicLinkState.clear(notes);
        assertFalse(MagicLinkState.linkStillValid(notes, hash, NOW));
    }
}
