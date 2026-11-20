package nl.minbzk.rig.keycloak.magiclink;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public class SendBudgetTest {

    private static final long NOW = 1_700_000_000_000L;

    @Test
    public void anEmptyBudgetAllowsASend() {
        assertTrue(SendBudget.EMPTY.allows(NOW));
    }

    @Test
    public void refusesASecondSendWithinThirtySeconds() {
        SendBudget budget = SendBudget.EMPTY.registerSend(NOW);
        assertFalse(budget.allows(NOW + 29_000L));
        assertTrue(budget.allows(NOW + 30_000L));
    }

    @Test
    public void refusesASixthSendWithinTheQuarter() {
        SendBudget budget = SendBudget.EMPTY;
        long moment = NOW;
        for (int i = 0; i < SendBudget.MAX_PER_WINDOW; i++) {
            assertTrue("send " + i + " should be allowed", budget.allows(moment));
            budget = budget.registerSend(moment);
            moment += SendBudget.MIN_INTERVAL_MILLIS;
        }
        assertFalse(budget.allows(moment));
        assertTrue(budget.allows(NOW + SendBudget.WINDOW_MILLIS));
    }

    @Test
    public void roundTripsThroughTheStoredForm() {
        SendBudget budget = SendBudget.EMPTY.registerSend(NOW).registerSend(NOW + 60_000L);
        assertEquals(budget, SendBudget.parse(budget.format()));
    }

    @Test
    public void unreadableStoredValueCountsAsNoHistory() {
        assertEquals(SendBudget.EMPTY, SendBudget.parse(null));
        assertEquals(SendBudget.EMPTY, SendBudget.parse(""));
        assertEquals(SendBudget.EMPTY, SendBudget.parse("rubbish"));
        assertEquals(SendBudget.EMPTY, SendBudget.parse("1:2"));
        assertEquals(SendBudget.EMPTY, SendBudget.parse("a:b:c"));
    }
}
