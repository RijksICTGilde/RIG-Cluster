/* De weg naar de zijbalk op een smal scherm.
 *
 * Staat de navigation split view in een kolom (base_lotc.html.j2), dan toont hij alleen het
 * hoofdpaneel. De terugknop in de titelbalk daarvan vuurt "back"; zonder has-content op het
 * hoofdpaneel kiest de split view zelf de zijbalk. Een klik in de zijbalk laadt een nieuwe
 * pagina, en die begint weer met het hoofdpaneel.
 */
document.addEventListener("back", function (event) {
    var paneel = event.target.closest && event.target.closest('nldd-split-view-pane[slot="main"]');
    if (paneel && paneel.parentElement && paneel.parentElement.tagName === "NLDD-NAVIGATION-SPLIT-VIEW") {
        paneel.removeAttribute("has-content");
    }
});
