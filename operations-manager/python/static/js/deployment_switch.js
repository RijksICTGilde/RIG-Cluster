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
     * Switch between deployment sections across all panels
     */
    window.switchDeployment = function (deploymentName) {
        // A hash can point at a deployment that no longer exists -- e.g. after deleting
        // it and reloading with '#deployments/<name>' still in the URL. Without this
        // guard the code below hides every section and fires a metrics request for a
        // deployment that is not there, producing a page for something that is gone.
        // Fall back to the first available deployment, or the overview when there is none.
        if (!document.getElementById('deployment-' + deploymentName)) {
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
        location.hash = 'deployments/' + deploymentName;
    };

    // Restore tab and deployment state from URL hash on page load.
    //
    // Stond als IIFE onderaan het script van project-details.html.j2. switchTab() bestaat
    // alleen op die pagina - de hertekende pagina heeft echte tab-URL's (?tab=...) - dus
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
            }
        } else if (hash === 'taken') {
            if (typeof switchTab === 'function') switchTab('taken');
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', restoreFromHash);
    } else {
        restoreFromHash();
    }
})();
