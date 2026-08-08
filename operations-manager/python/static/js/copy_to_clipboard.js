/**
 * copyToClipboard - de kopieerknop naast een waarde.
 *
 * Stond als inline functie in opi/templates/project-details.html.j2, terwijl hij door
 * vier templates buiten die pagina wordt aangeroepen (section-config, en de
 * dienstblokken van keycloak, invite en de OTP-code). Bij de omzetting naar de nieuwe
 * vormgeving was er een tweede aanroeper bijgekomen, en twee kopieen van dezelfde
 * functie lopen gegarandeerd uit de pas. Vandaar een bestand, net als edit_modal.js.
 *
 * De aanroepvorm is ONGEWIJZIGD overgenomen:
 *
 *     copyToClipboard('.config-code', event, '.config-item')
 *
 * De knop zoekt vanaf het aangeklikte element de dichtstbijzijnde container
 * (containerSelector), pakt daarbinnen de waarde (valueSelector) en zet die op het
 * klembord. Zo hoeft geen enkele waarde een eigen id te krijgen.
 *
 * EEN VERSCHIL, en waarom het er is
 *
 * De terugmelding ("Gekopieerd!") werd gezet met setAttribute('label', ...). Dat is de
 * naam die de ROOS-knop leest. De knop van het nieuwe thema is een <nldd-button> en
 * leest zijn opschrift uit 'text'. Daarom wordt nu het attribuut gezet dat de knop
 * ZELF al draagt; staat er geen van beide, dan valt hij terug op 'label'. Dat is
 * dezelfde terugmelding, niet een andere.
 */
(function () {
    'use strict';

    function setLabel(btn, value) {
        var attribute = btn.hasAttribute('text') ? 'text' : 'label';
        btn.setAttribute(attribute, value);
    }

    window.copyToClipboard = function (valueSelector, event, containerSelector) {
        var container = event.target.closest(containerSelector);
        if (!container) return;
        var codeEl = container.querySelector(valueSelector);
        if (!codeEl) return;

        navigator.clipboard.writeText(codeEl.textContent.trim()).then(function () {
            var btn = event.target.closest('.copy-btn');
            if (btn) {
                setLabel(btn, 'Gekopieerd!');
                btn.classList.add('is-copied');
                setTimeout(function () {
                    setLabel(btn, 'Kopieer');
                    btn.classList.remove('is-copied');
                }, 2000);
            }
        });
    };
})();
