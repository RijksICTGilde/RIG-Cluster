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

    document.addEventListener("htmx:afterSettle", function () {
        if (scrollY === null) return;
        var doel = scrollY;
        scrollY = null;
        // In de volgende frame: de browser heeft de nieuwe hoogte dan verwerkt, anders
        // klemt hij onze waarde af op een pagina die nog te kort is.
        requestAnimationFrame(function () {
            if (Math.abs(window.scrollY - doel) > 1) window.scrollTo(0, doel);
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
