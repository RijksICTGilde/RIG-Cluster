/**
 * Wisselen tussen de deployments op het tabblad Deployments.
 *
 * Stond als inline functie in opi/templates/project-details.html.j2 en is hierheen
 * verhuisd toen de hertekende pagina (templates_lotc/bg/project-tabs.html.j2) hem ook
 * nodig kreeg. Eén kopie, twee pagina's - net als openEditModal/openServiceModal in
 * edit_modal.js.
 *
 * De functie is ONGEWIJZIGD overgenomen. Ze hangt aan markup:
 *   - .deployment-section  : elk blok dat bij één deployment hoort
 *   - id="deployment-<naam>", "deployment-actions-<naam>", "argocd-<naam>"
 *   - .deployment-section[data-deployment="<naam>"] voor de blokken die de diensten
 *     leveren (die zeggen zelf bij welke deployment ze horen)
 *   - id="global-deployment-selector" : de keuzelijst die meeloopt
 * Wie die namen verandert, zet dit gedrag stil uit; ze zijn dus geen vormgeving.
 */
(function () {
    'use strict';

    /**
     * Is er op DEZE pagina een blok dat bij deze deployment hoort?
     *
     * Twee vormen, en allebei tellen ze: het tabblad Deployments zet de naam in de id
     * (deployment-<naam>), de blokken van de diensten en het tabblad Metrics zeggen het
     * met data-deployment. Alleen op de id kijken betekende dat de kiezer op Metrics
     * meteen afhaakte, want daar bestaat geen enkele deployment-<naam>.
     */
    function heeftBlok(deploymentName) {
        return !!(
            document.getElementById('deployment-' + deploymentName) ||
            document.querySelector('.deployment-section[data-deployment="' + CSS.escape(deploymentName) + '"]')
        );
    }

    /**
     * De gekozen deployment, onthouden zolang het browsertabblad open is.
     *
     * Elk tabblad van de projectpagina is een eigen pad (/projects/<tabblad>/<naam>), dus de keuze in de
     * URL-hash overleeft het wisselen van tabblad niet. Zonder dit moest je op Metrics
     * opnieuw kiezen wat je op Deployments net had gekozen. De sleutel is het pad, zodat
     * twee projecten elkaars keuze niet overschrijven.
     */
    var BEWAARSLEUTEL = 'zad:deployment:' + location.pathname;

    function bewaar(deploymentName) {
        try {
            sessionStorage.setItem(BEWAARSLEUTEL, deploymentName);
        } catch (e) {
            // Geen sessionStorage (privacystand, oude browser): de keuze wordt dan niet
            // onthouden en verder werkt alles hetzelfde.
        }
    }

    function bewaarde() {
        try {
            return sessionStorage.getItem(BEWAARSLEUTEL);
        } catch (e) {
            return null;
        }
    }

    /**
     * Switch between deployment sections across all panels
     */
    window.switchDeployment = function (deploymentName) {
        // A hash can point at a deployment that no longer exists -- e.g. after deleting
        // it and reloading with '#deployments/<name>' still in the URL. Without this
        // guard the code below hides every section and fires a metrics request for a
        // deployment that is not there, producing a page for something that is gone.
        // Fall back to the first available deployment, or the overview when there is none.
        if (!heeftBlok(deploymentName)) {
            var gsel = document.getElementById('global-deployment-selector');
            var alt = gsel && gsel.options.length ? gsel.options[0].value : null;
            if (alt && alt !== deploymentName) {
                deploymentName = alt;
            } else {
                location.hash = 'deployments';
                return;
            }
        }
        // Hide all deployment sections
        document.querySelectorAll('.deployment-section').forEach(section => {
            section.classList.add('is-hidden');
        });
        // Show sections matching this deployment
        ['deployment-actions-', 'deployment-', 'argocd-'].forEach(prefix => {
            const el = document.getElementById(prefix + deploymentName);
            if (el) el.classList.remove('is-hidden');
        });
        // Service-owned blocks (RC-24) say which deployment they belong to instead of
        // encoding it in an id prefix this list has to know about.
        document.querySelectorAll('.deployment-section[data-deployment="' + deploymentName + '"]')
            .forEach(el => { el.classList.remove('is-hidden'); });

        // Sync global selector and URL hash
        const gs = document.getElementById('global-deployment-selector');
        if (gs) gs.value = deploymentName;
        bewaar(deploymentName);
        // De hash zegt 'deployments/<naam>' en dat klopt alleen op het tabblad
        // Deployments. Op Metrics zou hij de lezer naar een ander tabblad wijzen; de
        // keuze staat daar in de opslag hierboven.
        if (document.getElementById('deployment-' + deploymentName)) {
            location.hash = 'deployments/' + deploymentName;
        }
    };

    // Restore tab and deployment state from URL hash on page load.
    //
    // Stond als IIFE onderaan het script van project-details.html.j2. switchTab() bestaat
    // alleen op die pagina - de hertekende pagina heeft echte tab-paden - dus
    // de aanroep is achter een typeof-controle gezet. Verder ongewijzigd.
    //
    // Draait op DOMContentLoaded en niet direct, want dit bestand wordt in de <head>
    // geladen en de blokken waar switchDeployment naar zoekt bestaan dan nog niet.
    function restoreFromHash() {
        var hash = location.hash.replace('#', '');
        if (hash.startsWith('deployments')) {
            if (typeof switchTab === 'function') switchTab('deployments');
            var parts = hash.split('/');
            if (parts.length > 1) {
                window.switchDeployment(parts[1]);
                return;
            }
        } else if (hash === 'taken') {
            if (typeof switchTab === 'function') switchTab('taken');
        }
        // Heeft de SERVER een deployment opengezet? Dan is dat de keuze. Sinds de
        // deploymenttabel op Overzicht de ingang is, opent een rij met een gewone link
        // (/projects/deployments/<project>?deployment=<naam>) en rendert de server dat
        // paneel zichtbaar. De opgeslagen keuze hieronder zou daar overheen gaan: je
        // klikt een rij aan en ziet een ander paneel.
        //
        // Alleen de HASH wint hier nog van, want die staat net zo expliciet in de URL en
        // wordt door switchDeployment() zelf geschreven bij het wisselen zonder herladen.
        var weergave = document.getElementById('deployments-weergave');
        var vanServer = weergave && weergave.getAttribute('data-deployment-open');
        if (vanServer && heeftBlok(vanServer)) {
            // De server heeft het paneel al zichtbaar gezet; dit zet alleen de opgeslagen
            // keuze gelijk, zodat het tabblad Metrics dezelfde deployment toont.
            bewaar(vanServer);
            return;
        }

        // Geen deployment in de URL: pak de keuze van het vorige tabblad, als die er is
        // en als hij op deze pagina bestaat. De hash wint, want die staat er expliciet.
        var eerder = bewaarde();
        if (eerder && heeftBlok(eerder)) {
            window.switchDeployment(eerder);
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', restoreFromHash);
    } else {
        restoreFromHash();
    }
})();
