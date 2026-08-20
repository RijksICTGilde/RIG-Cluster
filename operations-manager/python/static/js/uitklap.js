/**
 * Uitklappende rijen: het omzetten van de stand, meer niet.
 *
 * <nldd-list-item> brengt het uitklappen zelf al mee. Het draagt een `expanded`, het
 * verbergt en toont zijn slot "children" op die stand, en een <nldd-icon-cell disclosure>
 * erin draait mee. Wat het NIET doet is die stand omzetten bij een klik: `expanded` is
 * een eigenschap van de consument, zodat een rij ook door iets anders open gezet kan
 * worden (een route, een zoekterm, een bovenliggende rij).
 *
 * Wij zijn die consument, en dit is het hele stukje dat daarbij hoort. Geen eigen opmaak,
 * geen nagebouwd pijltje, geen tweede verborgen vlak: het component tekent, dit zegt
 * alleen wanneer.
 *
 * Gedelegeerd op het document, want de componentkaarten komen ook terug via htmx (na een
 * bewerking wordt het blok opnieuw ingevoegd). Een listener per rij zou dan opnieuw
 * gehangen moeten worden; deze niet.
 */
(function () {
    'use strict';

    document.addEventListener('click', function (event) {
        var rij = event.target.closest && event.target.closest('nldd-list-item[data-uitklap]');
        if (!rij) return;

        /* De bediening van een rij (het vraagteken bij een dienst, een knop in de inhoud)
           is een eigen besturingselement en mag de rij niet omklappen. */
        if (event.target.closest('nldd-list-item-action')) return;

        /* Een klik BINNEN het uitgeklapte deel is geen klik op de rij: daar staan
           invoervelden, kopieerknoppen en oogjes, en die moeten hun eigen werk doen
           zonder dat het blok onder hun vingers dichtklapt. */
        if (event.target.closest('[slot="children"]')) return;

        rij.expanded = !rij.expanded;
    });
})();
