package nl.minbzk.rig.keycloak.email;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import org.junit.Test;

/**
 * Houdt {@link RelayMailConfig#VERSION} vast aan de versie in {@code pom.xml}.
 *
 * <p>Waarom dat een eigen toets waard is: het merkteken op elk bericht
 * ({@code X-ZAD-Email-Sender: zad-relay/<versie>}) is het ENIGE wat in een postbus laat
 * zien welke code een bericht verstuurde. Loopt die versie achter op de jar die
 * daadwerkelijk draait, dan wijst het merkteken naar de verkeerde code en is het erger dan
 * geen merkteken. Er is geen andere plek die deze twee naast elkaar legt: de constante is
 * handgeschreven en {@code pom.xml} bepaalt de bestandsnaam van het artefact.
 *
 * <p>De bestandsnaam van de jar staat bovendien in de kustomize-generator en in de
 * initContainer van Keycloak. Een versiesprong is dus nooit alleen deze constante; de
 * README noemt de plekken.
 */
public class RelayVersieTest {

    /** De {@code <version>} van het project zelf: het eerste versie-element na het artifactId. */
    private static final Pattern PROJECTVERSIE =
            Pattern.compile("<artifactId>keycloak-relay-email-sender</artifactId>\\s*<version>([^<]+)</version>");

    @Test
    public void deConstanteIsDeVersieUitDePom() throws IOException {
        String pom = leesPom();

        Matcher m = PROJECTVERSIE.matcher(pom);
        assertTrue("kon <version> van het project niet vinden in pom.xml", m.find());

        assertEquals(
                "RelayMailConfig.VERSION loopt uit de pas met pom.xml; werk beide bij, plus de bestandsnaam "
                        + "van de jar in de kustomize-generator en de initContainer (zie README.md)",
                m.group(1),
                RelayMailConfig.VERSION);
    }

    /**
     * De jar wordt REPRODUCEERBAAR gebouwd en byte voor byte vergeleken in CI (zie de taak
     * {@code keycloak-relay-email-sender-controleren}). Dat kan alleen zolang de bouw geen
     * klok gebruikt, en dat is precies wat deze eigenschap uitzet. Verdwijnt hij, dan valt
     * die vergelijking om op elke commit - een rood dat niets over de code zegt.
     */
    @Test
    public void deBouwIsReproduceerbaar() throws IOException {
        assertTrue(
                "project.build.outputTimestamp ontbreekt in pom.xml: zonder die eigenschap is de jar niet "
                        + "reproduceerbaar en faalt de CI-vergelijking op elke commit",
                leesPom().contains("<project.build.outputTimestamp>"));
    }

    private static String leesPom() throws IOException {
        // Surefire draait met de modulemap als werkmap, dus pom.xml staat ernaast.
        Path pom = Paths.get("pom.xml").toAbsolutePath();
        assertTrue("pom.xml niet gevonden op " + pom, Files.exists(pom));
        return new String(Files.readAllBytes(pom), StandardCharsets.UTF_8);
    }
}
