/**
 * Zet de key-value editors op zodra htmx nieuwe inhoud heeft neergezet.
 *
 * De editors (omgevingsvariabelen, aliassen) worden door codemirror-kv.js opgebouwd, en
 * die kan dat pas als het veld in de DOM staat. In een DIALOOG komt dat veld pas binnen
 * NA het openen: de wizardstap wordt met htmx opgehaald. Zonder deze haak blijft er een
 * kale textarea staan die zich niet gedraagt zoals de rest van het formulier.
 *
 * afterSettle en niet afterSwap: dan is de DOM helemaal klaar en zijn de attributen
 * verwerkt. Dit is dezelfde haak die project-details.html.j2 inline heeft; hij staat hier
 * in een bestand omdat inmiddels twee pagina's hem nodig hebben, en twee kopieen lopen
 * uit de pas.
 */
document.addEventListener("htmx:afterSettle", function (evt) {
    if (typeof initKvEditors === "function") {
        initKvEditors(evt.detail.target);
    }
});
