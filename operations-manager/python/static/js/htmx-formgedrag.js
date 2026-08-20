/* Twee dingen die elk htmx-formulier in dit portaal nodig heeft.
 *
 * LET OP: de luisteraars hangen aan `document` en NIET aan `document.body`. Dit bestand
 * wordt in de <head> geladen (base_lotc.html.j2), en op dat moment bestaat document.body
 * nog niet. `document.body.addEventListener` gooide daar een TypeError, waarmee dit hele
 * script stierf voor het iets had gedaan: geen scrollherstel, geen dubbelklikbescherming.
 * In de console was dat te zien als "can't access property addEventListener,
 * document.body is null". htmx-gebeurtenissen bubbelen tot document, dus daar horen ze.
 *
 * 1. DE SCROLLPOSITIE BLIJFT STAAN.
 *    Een stap in de wizard vervangt bij elke keuze een groot deel van het formulier - vink
 *    je een dienst aan bij een component, dan komt het hele configuratieblok erbij. htmx
 *    scrollt na zo'n vervanging naar de bovenkant van wat het inruilde, dus je kijkt ineens
 *    ergens anders dan waar je net klikte. Dat is niet per scherm op te lossen met
 *    `hx-swap="... show:none"`, want het geldt voor elk formulier dat we hebben.
 *
 *    De positie wordt vlak voor de vervanging onthouden en na het opnieuw opbouwen
 *    teruggezet. Na `afterSettle` en niet na `afterSwap`: op dat eerste moment staan de
 *    web-componenten van het thema nog niet op hun eindhoogte, dus terugzetten landt dan
 *    op de verkeerde plek.
 *
 *    Alleen bij een vervanging die de pagina korter of langer maakt heeft dit effect; een
 *    swap die niets aan de hoogte verandert merkt hier niets van.
 *
 * 2. HET FORMULIER IS NIET AAN TE KLIKKEN TERWIJL HET ANTWOORD ONDERWEG IS.
 *    Zonder dit kun je tijdens een lopend verzoek doorklikken. Dat is niet theoretisch: htmx
 *    gooit een tweede verzoek weg als hetzelfde element nog bezig is, dus die klik verdwijnt
 *    geruisloos - de gebruiker ziet niets gebeuren en klikt nog eens. Precies dat kostte ons
 *    de wispelturige e2e-tests op de bewerkdialoog (zie TODO.md).
 *
 *    De klasse `is-bezig` gaat op het formulier dat het verzoek doet, en de CSS ernaast zet
 *    pointer-events uit en dimt het licht. Geen `disabled` op de velden: dat haalt ze uit de
 *    inzending, en dan verlies je bij een trage verbinding de helft van het formulier.
 */
(function () {
    "use strict";

    var scrollY = null;

    function formulierVan(el) {
        if (!el || !el.closest) return null;
        return el.closest("form") || el.closest("[data-htmx-blokkeer]");
    }

    document.addEventListener("htmx:beforeSwap", function () {
        scrollY = window.scrollY;
    });

    /* De pagina terug op de hoogte waar hij stond. In de volgende frame: de browser heeft
       de nieuwe hoogte dan verwerkt, anders klemt hij onze waarde af op een pagina die nog
       te kort is. */
    function herstelDeScroll() {
        if (scrollY === null) return;
        var doel = scrollY;
        scrollY = null;
        requestAnimationFrame(function () {
            if (Math.abs(window.scrollY - doel) > 1) window.scrollTo(0, doel);
        });
    }

    /* 3. DE FOCUS BLIJFT WAAR HIJ WAS.
     *
     * Bij zoeken-tijdens-typen wordt het zoekveld meegeswapt, en dan sta je na elke
     * toetsaanslag buiten het veld. hx-preserve lost dat hier niet op: de echte <input>
     * zit in de schaduwboom van nldd-search-field, en al verplaatst htmx de huls
     * ongewijzigd, de browser geeft de focus niet terug bij zo'n verplaatsing.
     *
     * Vandaar zelf onthouden en teruggeven. Op ID, want het element zelf is na de swap een
     * ander object. De cursorpositie gaat mee, anders springt hij naar het begin en typ je
     * je volgende letter op de verkeerde plek.
     *
     * Na afterSettle en niet na afterSwap, om dezelfde reden als het scrollen hierboven:
     * op dat eerste moment staan de web-componenten nog niet. */
    var focusId = null;
    var cursor = null;

    function actiefVeld() {
        var el = document.activeElement;
        /* Staat de focus in een schaduwboom, dan is activeElement de HOST; daar dalen we
           doorheen tot bij het invoerveld dat de focus echt heeft. Dat veld is waar de
           CURSOR staat - het id komt ergens anders vandaan, zie hieronder. */
        while (el && el.shadowRoot && el.shadowRoot.activeElement) el = el.shadowRoot.activeElement;
        return el;
    }

    document.addEventListener("htmx:beforeSwap", function () {
        /* Het id zoeken we vanaf `document.activeElement` en NIET vanaf het veld uit
           actiefVeld(). Dat is het hele verschil tussen wel en niet werken, en het is in de
           browser gemeten op /projects:

               document.activeElement  -> NLDD-SEARCH-FIELD#projects-zoekveld
               actiefVeld()            -> INPUT (in de schaduwboom, zonder id)
               INPUT.closest("[id]")   -> null

           `closest()` klimt namelijk NIET over de rand van een schaduwboom heen. Zocht je
           dus vanaf het echte invoerveld, dan vond je nooit een id - precies bij de velden
           waarvoor dit herstel geschreven is. focusId bleef null en afterSettle stapte er
           meteen weer uit; de focus viel op <body> en je kon niet verder typen.

           `document.activeElement` staat altijd in de LICHTE boom (bij focus in een
           schaduw is dat de host), dus daar werkt closest() gewoon. Voor een kaal
           <input id="..."> verandert er niets: dat IS activeElement. */
        var host = document.activeElement;
        var drager = host && host.closest ? host.closest("[id]") : null;
        var el = actiefVeld();
        focusId = drager ? drager.id : null;
        cursor = el && typeof el.selectionStart === "number" ? el.selectionStart : null;
    });

    /* Terug naar waar de cursor stond. Geeft true als dat gelukt is, zodat de regel
       hieronder weet of er nog iets te beslissen valt. */
    function herstelDeCursor() {
        if (!focusId) return false;
        var doel = document.getElementById(focusId);
        focusId = null;
        if (!doel) return false;
        var invoer = doel.shadowRoot ? doel.shadowRoot.querySelector("input, textarea") : doel;
        if (!invoer || !invoer.focus) return false;
        invoer.focus();
        if (cursor !== null && typeof invoer.setSelectionRange === "function") {
            try {
                invoer.setSelectionRange(cursor, cursor);
            } catch (e) {
                /* Een veldtype dat geen selectie kent; de focus is het belangrijkste. */
            }
        }
        cursor = null;
        return true;
    }

    /* De eerste fout in beeld, en de cursor erin.
     *
     * Na een afgekeurde inzending stond je nergens: de knop waarop je klikte is meegeswapt,
     * dus de focus valt op <body>. Je zag dan wel dat er een veld rood was en moest er
     * alsnog met de muis heen.
     *
     * Het veld is een <nldd-text-field> met aria-invalid="true", en die componenten draaien
     * op delegatesFocus (gemeten in de themabundel), dus focus() op de huls komt vanzelf in
     * het echte invoerveld terecht. preventScroll omdat de regel eronder het beeld rustig
     * verplaatst; focus() alleen springt naar de rand.
     *
     * Alleen een VELD krijgt de cursor. Staat er geen fout veld maar wel een foutregel of
     * een melding, dan wordt daar alleen naartoe gescrold: in tekst kun je niet typen. */
    function naarDeEersteFout(gebied) {
        var veld = gebied.querySelector('[aria-invalid="true"]');
        var doel = veld
            || gebied.querySelector(".rvo-form-field__error-text, .lotc-form-field__error-text")
            || gebied.querySelector('[data-roos-component="alert"]');
        if (!doel) return false;
        if (veld && typeof veld.focus === "function") veld.focus({ preventScroll: true });
        doel.scrollIntoView({ behavior: "smooth", block: "center" });
        return true;
    }

    /*
     * WAAR DE FOCUS HEEN GAAT NA EEN SWAP, OP EEN PLEK BESLIST.
     *
     * Dit stond even in twee bestanden: het herstel hier, en het springen naar de eerste
     * fout in static/js/wizard.js. Twee luisteraars op dezelfde gebeurtenis die allebei de
     * focus zetten, waarbij de laatst geregistreerde won - en dat betekende dat je op een
     * stap met een openstaande fout bij elke swap uit je veld werd getrokken.
     *
     * Het is een keuze en geen stapeling, dus staat hij hier als een keuze:
     *
     *   1. stond de cursor ergens en bestaat dat element nog -> terug daarheen;
     *   2. staat de focus nog ergens anders -> daar blijven;
     *   3. anders, is er een fout -> naar de eerste, en in beeld;
     *   4. anders -> niets.
     *
     * En het SCROLLEN hoort bij diezelfde beslissing, want ook daar liepen er twee door
     * elkaar. Standaard gaat de pagina terug naar de hoogte waar hij stond; alleen als we
     * naar de fout springen blijft dat achterwege, want anders trekt het herstel het beeld
     * meteen weer terug.
     *
     * Zoeken-tijdens-typen valt onder 1 (het veld swapt mee bij elke toetsaanslag), een
     * afgekeurde inzending onder 3.
     *
     * Stap 2 lijkt overbodig naast 1 en is het niet: het herstel werkt op een ID, en een
     * veld zonder id levert er geen. De focus staat dan gewoon nog waar hij stond - de swap
     * raakte dat element niet - en die mag er net zo goed niet weggetrokken worden. Zonder
     * deze stap gebeurt dat wel, en dan is de klacht terug in een geval dat lastiger te
     * vinden is.
     */
    document.addEventListener("htmx:afterSettle", function (e) {
        var gebied = e && e.detail && e.detail.target && e.detail.target.querySelector ? e.detail.target : document;

        var cursorTerug = herstelDeCursor();
        var actief = document.activeElement;
        var iemandStaatErgens = actief && actief !== document.body && actief !== document.documentElement;

        if (!cursorTerug && !iemandStaatErgens && naarDeEersteFout(gebied)) {
            /* We zijn naar de fout gegaan. De oude hoogte NIET terugzetten: dat is precies
               wat er misging toen deze twee nog los van elkaar liepen. Het scrollherstel
               forceerde in de volgende frame de oude positie terug, en dat won van het
               scrollIntoView dat een tel eerder begon - de cursor stond in het foute veld
               en het beeld sprong terug naar waar je vandaan kwam. */
            scrollY = null;
            return;
        }

        herstelDeScroll();
    });

    /* 4. EEN THEMACOMPONENT MET width= HOUDT ZIJN BREEDTE NA EEN SWAP.
     *
     * Gemeten op /projects, met exact dezelfde markup voor en na:
     *
     *     eerste keer laden : huls 416px, inline stijl "--_width: 26rem;"
     *     na de htmx-swap   : huls 321px, inline stijl WEG, width="26rem" nog aanwezig
     *
     * De nldd-componenten zijn Lit-elementen die hun width-ATTRIBUUT in `updated()`
     * vertalen naar de CSS-variabele --_width. Na een swap blijft het attribuut staan maar
     * gebeurt die vertaling niet opnieuw, en dan valt het element terug op de breedte van
     * zijn inhoud. Zichtbaar als: het zoekveld wordt smaller zodra je zoekt of sorteert.
     *
     * Wat hier gebeurt is NIET de variabele van het component van buitenaf zetten - die
     * begint met een underscore, dat is zijn eigen keuken, en zoiets is precies het soort
     * CSS-omweg dat we niet willen. Het element wordt gevraagd zijn eigen afleiding
     * opnieuw te doen, via requestUpdate(), de publieke Lit-API daarvoor. Zonder tweede
     * argument ziet Lit geen wijziging, dus de oude waarde gaat expliciet mee.
     *
     * Dit hoort in het component thuis en is als zodanig gemeld; zie
     * request_for_components.md. Zolang dat niet rond is, staat het hier. */
    document.addEventListener("htmx:afterSettle", function (e) {
        var gebied = e && e.target && e.target.querySelectorAll ? e.target : document;
        gebied.querySelectorAll("[width]").forEach(function (el) {
            if (el.tagName.lastIndexOf("NLDD-", 0) !== 0) return;
            if (typeof el.requestUpdate !== "function") return;
            el.requestUpdate("width", undefined);
        });
    });

    document.addEventListener("htmx:beforeRequest", function (e) {
        var f = formulierVan(e.target);
        if (f) f.classList.add("is-bezig");
    });

    function klaar(e) {
        var f = formulierVan(e.target);
        if (f) f.classList.remove("is-bezig");
        // Vangnet: een verzoek dat afbreekt zonder afterRequest zou het formulier anders
        // blijvend blokkeren.
        document.querySelectorAll("form.is-bezig").forEach(function (el) {
            if (!el.classList.contains("htmx-request")) el.classList.remove("is-bezig");
        });
    }

    document.addEventListener("htmx:afterRequest", klaar);
    document.addEventListener("htmx:responseError", klaar);
    document.addEventListener("htmx:sendError", klaar);
    document.addEventListener("htmx:timeout", klaar);
})();
