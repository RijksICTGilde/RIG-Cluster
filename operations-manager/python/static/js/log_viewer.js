/**
 * De logviewer: het schuifpaneel met de live logstroom van een component.
 *
 * Deze code stond in opi/templates/project-details.html.j2 en is daar weggehaald toen de
 * nieuwe vormgeving dezelfde viewer moest kunnen openen. Ze hoort niet bij een pagina
 * maar bij het paneel: de markup staat in opi/templates_lotc/bg/_log-viewer.html.j2 en
 * deze code zoekt zijn elementen daar op id op.
 *
 * LET OP: dit script pakt zijn elementen op het moment dat het draait, niet later. Het
 * moet dus NA de markup van het paneel geladen worden, onderaan de pagina. Laad je het in
 * de <head>, dan zijn alle verwijzingen null en doet de viewer stilzwijgend niets.
 *
 * WAT ER VERANDERD IS TOEN HET PANEEL EEN THEMACOMPONENT WERD
 *
 * Het paneel is een <nldd-sheet>. Dat is geen kosmetische wissel voor deze code, want
 * een sheet is een native <dialog>: hij gaat open en dicht met show() en hide() in plaats
 * van met een klasse, hij tekent zijn eigen waas (de losse #log-viewer-backdrop is weg),
 * hij houdt de pagina eronder vanzelf stil (document.body.style.overflow hoeft niet
 * meer), en hij sluit zelf op Escape en op een klik ernaast.
 *
 * Dat laatste is de reden dat sluiten hier via het 'close'-event van de sheet loopt en
 * niet alleen via closeLogViewer(). Er zijn nu VIER manieren om het paneel dicht te doen -
 * de knop Sluiten, Escape, een klik naast het paneel, en closeLogViewer() zelf - en er is
 * er maar een die de WebSocket mag opruimen. Hing die opruiming aan de knop, dan liet
 * Escape een pod achter die blijft streamen.
 *
 * De besturingselementen zijn componenten geworden, en die dragen hun toestand op een
 * eigen property in plaats van op een klasse of op .checked:
 *
 *   - het zoekveld is <nldd-search-field>: .value, net als een input
 *   - de niveaufilters zijn <nldd-toggle-button>: .selected in plaats van .checked
 *   - de regelterugloop is <nldd-switch-field>: .checked
 *   - de pauzeknop is <nldd-toggle-button>: .selected in plaats van de klasse .is-active
 *   - de componentkiezer is nog steeds een echte <select>, in een <nldd-dropdown>
 *
 * DE PODKIEZER EN DE VORIGE-POGING-SCHAKELAAR (RC-162)
 *
 * Zonder podnaam volgt de server een label-selector, en die levert de regels van ELKE
 * matchende pod door elkaar heen zonder erbij te zeggen welke regel bij welke pod hoort.
 * Bij een mislukte uitrol staan er twee pods: een die bedient en een die crasht. "Alle
 * pods" is die oude stand en blijft de eerste optie; de rest van de lijst komt van
 * /api/logs/pods, dat zelf beoordeelt welke pods deze gebruiker mag lezen.
 *
 * De vorige poging is geen lopende stroom maar een AFGESLOTEN logboek: de container is
 * gestopt, de API levert wat er bewaard is en het proces eindigt. Dat staat daarom in de
 * statusregel, anders leest een stilstaand paneel als een kapotte verbinding.
 */
/**
 * Log Viewer WebSocket Client
 */
(function() {
    // Log viewer state
    let logSocket = null;
    let logPaused = false;
    let currentProject = null;
    let currentDeployment = null;
    let currentComponent = null;
    let components = [];
    let logLines = [];
    const MAX_LOG_LINES = 10000;

    // De gekozen pod, of null voor "Alle pods" - de label-selector, en het gedrag dat er
    // voor RC-162 als enige was. En of we naar de VORIGE poging van die pod kijken.
    let currentPod = null;
    let previousAttempt = false;
    // En wat de LOPENDE stroom draagt. Twee variabelen en niet een, omdat de kiezer en de
    // verbinding uit elkaar kunnen lopen: de podlijst komt na het openen binnen en zet dan
    // de standaardkeuze, terwijl de WebSocket al op de label-selector staat. Wie alleen de
    // kiezer bijhoudt ziet die twee standen als gelijk (nieuw === currentPod) en stuurt de
    // wissel nooit - dan noemt het paneel een pod terwijl alle pods door elkaar binnenkomen.
    let streamPod = null;
    let streamPrevious = false;
    // De pods zoals het endpoint ze laatst gaf, op naam. De schakelaar "vorige poging"
    // leest hieruit of de gekozen pod er een HEEFT.
    let podsByName = {};

    // DOM elements
    const panel = document.getElementById('log-viewer-panel');
    const heading = document.getElementById('log-viewer-heading');
    const componentSelector = document.getElementById('log-component-selector');
    const podSelector = document.getElementById('log-pod-selector');
    const previousToggle = document.getElementById('log-previous-attempt');
    const statusIndicator = document.getElementById('log-status-indicator');
    const statusText = document.getElementById('log-status-text');
    const lineCount = document.getElementById('log-line-count');
    const content = document.getElementById('log-viewer-content');
    const emptyState = document.getElementById('log-empty-state');
    const pauseBtn = document.getElementById('log-pause-btn');

    // Filter elements
    const searchInput = document.getElementById('log-search-input');
    const searchCount = document.getElementById('log-search-count');
    const filterError = document.getElementById('filter-error');
    const filterWarn = document.getElementById('filter-warn');
    const filterInfo = document.getElementById('filter-info');
    const filterDebug = document.getElementById('filter-debug');
    const wordWrapToggle = document.getElementById('log-word-wrap');

    /**
     * Staat het paneel open?
     *
     * Bijgehouden in plaats van afgelezen: <nldd-sheet> heeft geen publieke open-property,
     * en de <dialog> waar het aan af te lezen valt zit in zijn shadow root. Daar naar
     * binnen grijpen zou deze code laten breken op een interne wijziging van het thema.
     */
    let panelOpen = false;

    // Staat het paneel niet op deze pagina, dan houdt het hier op. Dat moet EXPLICIET:
    // hieronder wordt meteen een luisteraar op het paneel gezet, en op null gooit dat een
    // TypeError die de rest van dit bestand meesleurt - inclusief window.openLogViewer,
    // dat dan nergens meer bestaat. Voorheen viel dat niet op omdat alle verwijzingen pas
    // binnen een functie werden aangeraakt.
    if (!panel) {
        console.warn('log_viewer.js: het logpaneel staat niet op deze pagina');
        return;
    }

    /**
     * Ruim de stroom op. Loopt via het 'close'-event van de sheet, zodat het niet
     * uitmaakt HOE hij dichtgaat: de knop, Escape en een klik ernaast komen hier
     * allemaal langs, en closeLogViewer() ook.
     */
    panel.addEventListener('close', function() {
        panelOpen = false;

        if (logSocket) {
            logSocket.close();
            logSocket = null;
        }

        logPaused = false;
        pauseBtn.selected = false;
        pauseBtn.icon = 'pause';
        pauseBtn.setAttribute('accessible-label', 'Pauzeren');
    });

    /**
     * Open the log viewer panel
     */
    window.openLogViewer = function(project, deployment, component, comps, pod) {
        currentProject = project;
        currentDeployment = deployment;
        currentComponent = component;
        components = comps || [];
        // Een pod meegeven opent het paneel meteen op DIE pod. De knop naast een podregel
        // op de deploymentkaart doet dat; de algemene knop "Logs bekijken" niet, en die
        // blijft dus op "Alle pods" openen.
        currentPod = pod || null;
        previousAttempt = false;
        // De verbinding die zo opgezet wordt draagt precies deze stand. Hier al gelijk
        // zetten, zodat het vullen van de kiezer hieronder geen wissel uitlokt naar iets
        // wat er toch al op gaat.
        streamPod = currentPod;
        streamPrevious = previousAttempt;

        // Update UI. De kop is een <nldd-top-title-bar>: die draagt zijn tekst op
        // properties en niet in kindelementen, dus geen textContent maar .text/.supportingText.
        heading.text = `Logs - ${deployment}`;
        heading.supportingText = deployment;

        // Populate component selector
        componentSelector.innerHTML = '';
        components.forEach(comp => {
            const option = document.createElement('option');
            option.value = comp.reference;
            option.textContent = comp.reference;
            if (comp.reference === component) {
                option.selected = true;
            }
            componentSelector.appendChild(option);
        });
        // <nldd-dropdown> tekent de gekozen tekst zelf en werkt die alleen bij op
        // 'slotchange' en op een 'change' van de select. Opties toevoegen aan een select
        // die er al in zit doet geen van beide, dus zonder dit bericht blijft de lijst
        // er LEEG uitzien terwijl er componenten in staan. switchLogComponent() kan hier
        // geen kwaad: currentComponent staat hierboven al op dezelfde waarde en die
        // functie keert dan meteen terug.
        componentSelector.dispatchEvent(new Event('change', {bubbles: true}));

        // De podlijst komt van de server en is er dus nog niet; zet de kiezer alvast neer.
        // NIET met renderPodOptions: die gooit een pod weg die niet in zijn lijst staat, en
        // op een lege lijst is dat altijd - ook de pod die de kaartknop net meegaf.
        renderPodPlaceholder();
        loadPods();

        // Clear previous logs
        clearLogs();

        // Show panel. De sheet tekent zijn eigen waas en houdt de pagina eronder stil.
        panelOpen = true;
        panel.show();

        // Connect WebSocket
        connectLogWebSocket();
    };

    /**
     * Close the log viewer panel
     *
     * Alleen dichtdoen: het opruimen van de stroom hangt aan het 'close'-event hierboven,
     * zodat Escape en een klik naast het paneel dezelfde opruiming krijgen.
     */
    window.closeLogViewer = function() {
        panel.hide();
    };

    /**
     * Haal de pods van het huidige component op en vul de kiezer.
     *
     * Bij het openen en bij elke componentwissel, want een pod hoort bij een component.
     * Het endpoint doet ZELF de vraag of deze gebruiker deze pods mag lezen - dezelfde
     * functie die de WebSocket gebruikt om een podnaam te toetsen - dus wat hier
     * binnenkomt is per definitie wat er te kiezen valt.
     *
     * Mislukt het ophalen, dan blijft de kiezer staan zoals hij stond: "Alle pods", of de
     * pod waarop de kaartknop geopend heeft. Die keuze mag een mislukte lijst niet kosten -
     * de server toetst de podnaam zelf, en zonder pod kijk je weer naar alle pods door
     * elkaar heen, wat precies het gedrag is dat deze kiezer opheft.
     */
    function loadPods() {
        const component = currentComponent;
        const url = `/api/logs/pods/${encodeURIComponent(currentProject)}`
            + `?deployment=${encodeURIComponent(currentDeployment)}`
            + `&component=${encodeURIComponent(component)}`;

        fetch(url, {credentials: 'same-origin'})
            .then(response => response.ok ? response.json() : Promise.reject(response.status))
            .then(data => {
                // Er kan intussen van component gewisseld zijn; dan gaat dit antwoord over
                // een lijst die niemand meer op het scherm heeft staan.
                if (component !== currentComponent) return;
                renderPodOptions(data.pods || []);
            })
            .catch(err => {
                console.warn('Kon de pods niet ophalen:', err);
                if (component === currentComponent) renderPodPlaceholder();
            });
    }

    /**
     * Een leesbaar label voor een pod: het staartje van de naam plus wat hij doet.
     *
     * De volle naam is `<deployment>-<component>-<replicaset>-<pod>` en het enige deel dat
     * de pods onderling onderscheidt is het staartje. De rest is voor elke pod in deze
     * lijst hetzelfde en duwt juist het verschil van het scherm af.
     */
    function podLabel(pod) {
        const staart = '...' + pod.name.slice(-6);
        if (!pod.ready) {
            const herstarts = pod.restart_count
                ? `, ${pod.restart_count} herstart${pod.restart_count === 1 ? '' : 'en'}`
                : '';
            return `${staart}, start niet${herstarts}`;
        }
        if (pod.running_since) {
            const sinds = new Date(pod.running_since).toLocaleDateString('nl-NL', {day: 'numeric', month: 'short'});
            return `${staart}, draait sinds ${sinds}`;
        }
        return `${staart}, draait`;
    }

    /**
     * Vul de podkiezer, en kies de pod die de gebruiker waarschijnlijk zoekt.
     *
     * Standaard de NIET-GEREDE pod als die er is: dat is de pod die niet opkomt, en dus de
     * enige waar je logs voor opent. Is die er niet, dan "Alle pods" - het gedrag van
     * voor deze kiezer.
     */
    function renderPodOptions(pods) {
        podsByName = {};
        pods.forEach(pod => { podsByName[pod.name] = pod; });

        podSelector.innerHTML = '';
        const alle = document.createElement('option');
        alle.value = '';
        alle.textContent = 'Alle pods';
        podSelector.appendChild(alle);

        pods.forEach(pod => {
            const option = document.createElement('option');
            option.value = pod.name;
            option.textContent = podLabel(pod);
            podSelector.appendChild(option);
        });

        if (currentPod && !podsByName[currentPod]) {
            // De pod waar we op geopend zijn staat er niet (meer) in; niet stil op een
            // naam blijven staan die de server gaat weigeren.
            currentPod = null;
        }
        if (currentPod === null) {
            const kapot = pods.find(pod => !pod.ready);
            currentPod = kapot ? kapot.name : null;
        }
        podSelector.value = currentPod || '';

        // VOOR de change en niet erna: de schakelaar zet previousAttempt, en de wissel
        // hieronder vergelijkt juist die stand met wat de stroom draagt. Andersom stuurt
        // hij de vorige-poging-stand een ronde te laat, of helemaal niet.
        syncPreviousToggle();

        // Zonder deze change tekent <nldd-dropdown> de gekozen tekst niet bij en oogt de
        // lijst leeg terwijl er pods in staan. Zelfde reden als bij de componentkiezer.
        // En hij doet meer dan tekenen: switchLogPod zet de keuze die hierboven gevallen
        // is ook echt op de verbinding. Een keuze die alleen in de kiezer staat is geen
        // keuze - dan noemt het paneel een pod terwijl de server nog alle pods stuurt.
        podSelector.dispatchEvent(new Event('change', {bubbles: true}));
    }

    /**
     * Zet de kiezer neer voordat de podlijst binnen is.
     *
     * Het paneel gaat meteen open en verbindt meteen, dus er is op dat moment al een
     * keuze: de pod die de kaartknop meegaf, of "Alle pods". Die keuze mag hier NIET
     * sneuvelen - renderPodOptions gooit een pod weg die niet in zijn lijst staat, en met
     * een lege lijst is dat altijd, ook de pod waar net op geklikt is. Zodra de echte
     * lijst er is neemt renderPodOptions het over.
     */
    function renderPodPlaceholder() {
        podsByName = {};
        podSelector.innerHTML = '';
        const alle = document.createElement('option');
        alle.value = '';
        alle.textContent = 'Alle pods';
        podSelector.appendChild(alle);
        if (currentPod) {
            const option = document.createElement('option');
            option.value = currentPod;
            option.textContent = '...' + currentPod.slice(-6);
            podSelector.appendChild(option);
        }
        podSelector.value = currentPod || '';
        syncPreviousToggle();
        podSelector.dispatchEvent(new Event('change', {bubbles: true}));
    }

    /**
     * Zet de schakelaar "vorige poging" in de stand die bij de gekozen pod hoort.
     *
     * Alleen bedienbaar met een gekozen pod die zo'n poging HEEFT: --previous geldt per
     * container, dus op "Alle pods" bestaat de vraag niet. En hij staat standaard AAN bij
     * een pod die niet gereed is en herstarts heeft: dat is precies het geval waarin de
     * live stroom leeg blijft, omdat de container tussen twee pogingen in een
     * backoff-venster van minuten zit.
     */
    function syncPreviousToggle() {
        const pod = currentPod ? podsByName[currentPod] : null;
        const beschikbaar = !!(pod && pod.has_previous_attempt);

        previousToggle.disabled = !beschikbaar;
        if (!beschikbaar) {
            previousAttempt = false;
        } else if (pod && !pod.ready && pod.restart_count > 0) {
            previousAttempt = true;
        }
        previousToggle.checked = previousAttempt;
    }

    /**
     * Zeg wat er in het paneel te zien is, met de vorige-poging-stand erin verwerkt.
     *
     * Een afgesloten logboek groeit niet meer. Zonder dat erbij te zetten leest een
     * paneel dat stilstaat als een kapotte verbinding, en gaat iemand een storing zoeken
     * die er niet is.
     */
    function streamingStatusText(basis) {
        if (previousAttempt) {
            return 'Vorige poging - afgesloten logboek, dit groeit niet meer';
        }
        return basis;
    }

    /**
     * Connect to the WebSocket log stream
     */
    function connectLogWebSocket() {
        updateStatus('connecting', 'Connecting...');

        // Wat deze verbinding draagt. De url hieronder wordt hier letterlijk uit gebouwd.
        streamPod = currentPod;
        streamPrevious = previousAttempt;

        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        let wsUrl = `${protocol}//${window.location.host}/api/logs/stream/${currentProject}?deployment=${encodeURIComponent(currentDeployment)}&component=${encodeURIComponent(currentComponent)}&lines=250`;
        if (currentPod) {
            wsUrl += `&pod=${encodeURIComponent(currentPod)}`;
            if (previousAttempt) {
                wsUrl += '&previous=true';
            }
        }

        logSocket = new WebSocket(wsUrl);

        logSocket.onopen = function() {
            updateStatus('connecting', 'Connected, waiting for logs...');
        };

        logSocket.onmessage = function(event) {
            try {
                const data = JSON.parse(event.data);
                handleLogMessage(data);
            } catch (e) {
                console.error('Failed to parse log message:', e);
            }
        };

        logSocket.onclose = function(event) {
            if (event.code === 1000) {
                updateStatus('paused', 'Connection closed');
            } else {
                updateStatus('error', `Disconnected (code: ${event.code})`);
            }
        };

        logSocket.onerror = function(error) {
            console.error('WebSocket error:', error);
            updateStatus('error', 'Connection error');
        };
    }

    /**
     * Handle incoming log messages
     */
    function handleLogMessage(data) {
        switch (data.type) {
            case 'log':
                appendLogLine(data);
                break;
            case 'status':
                if (data.status === 'streaming') {
                    // De server mag de vorige-poging-stand corrigeren: heeft de pod geen
                    // vorige poging, dan valt hij terug op de gewone stroom en zegt dat
                    // in zijn bericht. Dan hoort de schakelaar dat ook te weten.
                    if (data.previous === false && previousAttempt) {
                        previousAttempt = false;
                        streamPrevious = false;
                        previousToggle.checked = false;
                    }
                    updateStatus('streaming', streamingStatusText(data.message || 'Streaming logs...'));
                } else if (data.status === 'paused') {
                    updateStatus('paused', data.message || 'Paused');
                } else if (data.status === 'connected') {
                    updateStatus('connecting', data.message || 'Connected');
                } else if (data.status === 'switching') {
                    updateStatus('connecting', data.message || 'Switching...');
                }
                if (data.component) {
                    currentComponent = data.component;
                    componentSelector.value = data.component;
                }
                if (Object.prototype.hasOwnProperty.call(data, 'pod')) {
                    currentPod = data.pod || null;
                    streamPod = currentPod;
                    podSelector.value = currentPod || '';
                }
                break;
            case 'error':
                updateStatus('error', data.message || 'Error');
                break;
        }
    }

    /**
     * Detect log level from line content
     */
    function detectLogLevel(line) {
        if (/\berror\b|\bERROR\b|\bfatal\b|\bFATAL\b|\bpanic\b|\bPANIC\b|\[STDERR\]/i.test(line)) {
            return 'error';
        } else if (/\bwarn\b|\bWARN\b|\bwarning\b|\bWARNING\b/i.test(line)) {
            return 'warn';
        } else if (/\binfo\b|\bINFO\b/i.test(line)) {
            return 'info';
        } else if (/\bdebug\b|\bDEBUG\b|\btrace\b|\bTRACE\b/i.test(line)) {
            return 'debug';
        }
        return 'other';
    }

    /**
     * Check if a log line should be visible based on current filters
     */
    function shouldShowLine(logData) {
        const level = logData.level || 'other';

        // Check level filters. .selected en niet .checked: dit zijn <nldd-toggle-button>'s.
        if (level === 'error' && !filterError.selected) return false;
        if (level === 'warn' && !filterWarn.selected) return false;
        if (level === 'info' && !filterInfo.selected) return false;
        if (level === 'debug' && !filterDebug.selected) return false;

        // Check search filter
        const searchTerm = (searchInput.value || '').toLowerCase().trim();
        if (searchTerm && !logData.line.toLowerCase().includes(searchTerm)) {
            return false;
        }

        return true;
    }

    /**
     * Create a log line DOM element
     */
    function createLogLineElement(data, searchTerm) {
        const lineEl = document.createElement('div');
        lineEl.className = 'log-line';

        const level = data.level || 'other';
        if (level === 'error') lineEl.classList.add('log-error');
        else if (level === 'warn') lineEl.classList.add('log-warn');
        else if (level === 'info') lineEl.classList.add('log-info');
        else if (level === 'debug') lineEl.classList.add('log-debug');

        // Apply search highlighting if there's a search term
        if (searchTerm) {
            const line = data.line || '';
            const lowerLine = line.toLowerCase();
            const lowerSearch = searchTerm.toLowerCase();
            let result = '';
            let lastIndex = 0;
            let index = lowerLine.indexOf(lowerSearch);

            while (index !== -1) {
                // Add text before match
                result += escapeHtml(line.substring(lastIndex, index));
                // Add highlighted match
                result += `<span class="search-highlight">${escapeHtml(line.substring(index, index + searchTerm.length))}</span>`;
                lastIndex = index + searchTerm.length;
                index = lowerLine.indexOf(lowerSearch, lastIndex);
            }
            // Add remaining text
            result += escapeHtml(line.substring(lastIndex));
            lineEl.innerHTML = result;
        } else {
            lineEl.textContent = data.line || '';
        }

        // Check if should be hidden
        if (!shouldShowLine(data)) {
            lineEl.classList.add('is-hidden');
        }

        return lineEl;
    }

    /**
     * Escape HTML for safe insertion
     */
    function escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    /**
     * Append a log line to the viewer
     */
    function appendLogLine(data) {
        // Hide empty state on first log
        if (logLines.length === 0) {
            emptyState.classList.add('is-hidden');
        }

        // Detect and store log level with the data
        const line = data.line || '';
        data.level = detectLogLevel(line);

        // Store log line
        logLines.push(data);

        // Remove old lines if over limit
        if (logLines.length > MAX_LOG_LINES) {
            logLines.shift();
            const firstLine = content.querySelector('.log-line');
            if (firstLine) {
                firstLine.remove();
            }
        }

        // Create log line element
        const searchTerm = (searchInput.value || '').trim();
        const lineEl = createLogLineElement(data, searchTerm);

        // Append and auto-scroll
        content.appendChild(lineEl);
        if (!logPaused && shouldShowLine(data)) {
            content.scrollTop = content.scrollHeight;
        }

        // Update line count
        updateLineCount();
    }

    /**
     * Update the visible/total line count display
     */
    function updateLineCount() {
        const visibleCount = content.querySelectorAll('.log-line:not(.is-hidden)').length;
        const totalCount = logLines.length;
        if (visibleCount === totalCount) {
            lineCount.textContent = `${totalCount} lines`;
        } else {
            lineCount.textContent = `${visibleCount} / ${totalCount} lines`;
        }
    }

    /**
     * Update the status indicator
     */
    function updateStatus(status, message) {
        statusIndicator.className = 'log-status-indicator';
        if (status === 'streaming') {
            statusIndicator.classList.add('is-streaming');
        } else if (status === 'paused') {
            statusIndicator.classList.add('is-paused');
        } else if (status === 'error') {
            statusIndicator.classList.add('is-error');
        } else if (status === 'connecting') {
            statusIndicator.classList.add('is-connecting');
        }
        statusText.textContent = message;
    }

    /**
     * Filter logs based on search term and level filters
     */
    window.filterLogs = function() {
        const searchTerm = (searchInput.value || '').trim();
        const logLineEls = content.querySelectorAll('.log-line');
        let matchCount = 0;

        // De wisknop is die van <nldd-search-field> zelf; het component toont hem zodra
        // er iets in staat. De eigen .log-search-clear met .is-visible is daarmee weg.

        // Re-render all log lines to apply/remove highlighting and filters
        logLineEls.forEach((el, index) => {
            if (index < logLines.length) {
                const data = logLines[index];
                const shouldShow = shouldShowLine(data);

                if (shouldShow) {
                    el.classList.remove('is-hidden');
                    matchCount++;

                    // Update highlighting
                    if (searchTerm) {
                        const line = data.line || '';
                        const lowerLine = line.toLowerCase();
                        const lowerSearch = searchTerm.toLowerCase();
                        let result = '';
                        let lastIndex = 0;
                        let idx = lowerLine.indexOf(lowerSearch);

                        while (idx !== -1) {
                            result += escapeHtml(line.substring(lastIndex, idx));
                            result += `<span class="search-highlight">${escapeHtml(line.substring(idx, idx + searchTerm.length))}</span>`;
                            lastIndex = idx + searchTerm.length;
                            idx = lowerLine.indexOf(lowerSearch, lastIndex);
                        }
                        result += escapeHtml(line.substring(lastIndex));
                        el.innerHTML = result;
                    } else {
                        el.textContent = data.line || '';
                    }
                } else {
                    el.classList.add('is-hidden');
                }
            }
        });

        // Update search count
        if (searchTerm) {
            searchCount.textContent = `${matchCount} match${matchCount !== 1 ? 'es' : ''}`;
        } else {
            searchCount.textContent = '';
        }

        // Update line count
        updateLineCount();
    };

    /**
     * Toggle word wrap
     */
    window.toggleWordWrap = function() {
        if (wordWrapToggle.checked) {
            content.classList.add('word-wrap');
        } else {
            content.classList.remove('word-wrap');
        }
    };

    /**
     * Toggle log pause/resume
     */
    window.toggleLogPause = function() {
        logPaused = !logPaused;

        if (logSocket && logSocket.readyState === WebSocket.OPEN) {
            logSocket.send(JSON.stringify({
                action: logPaused ? 'pause' : 'resume'
            }));
        }

        // De pauzestand staat op het component zelf (aria-pressed), niet op een eigen
        // klasse, en het icoon vertelt wat de knop NU doet.
        pauseBtn.selected = logPaused;
        pauseBtn.icon = logPaused ? 'play' : 'pause';
        pauseBtn.setAttribute('accessible-label', logPaused ? 'Hervatten' : 'Pauzeren');

        if (logPaused) {
            updateStatus('paused', 'Paused');
        } else {
            updateStatus('streaming', streamingStatusText('Streaming logs...'));
            // Scroll to bottom when resuming
            content.scrollTop = content.scrollHeight;
        }
    };

    /**
     * Clear all logs
     */
    window.clearLogs = function() {
        logLines = [];
        content.innerHTML = '';
        content.appendChild(emptyState);
        emptyState.classList.remove('is-hidden');
        emptyState.querySelector('p').textContent = 'No logs yet...';
        lineCount.textContent = '';

        // Reset search
        searchInput.value = '';
        searchCount.textContent = '';
    };

    /**
     * Copy logs to clipboard
     */
    window.copyLogs = function() {
        const text = logLines.map(l => l.line).join('\n');
        navigator.clipboard.writeText(text).then(() => {
            const originalText = statusText.textContent;
            statusText.textContent = 'Copied to clipboard!';
            setTimeout(() => {
                statusText.textContent = originalText;
            }, 2000);
        }).catch(err => {
            console.error('Failed to copy:', err);
        });
    };

    /**
     * Download logs as file
     */
    window.downloadLogs = function() {
        const text = logLines.map(l => l.line).join('\n');
        const blob = new Blob([text], { type: 'text/plain' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `logs-${currentDeployment}-${currentComponent}-${new Date().toISOString().slice(0,19).replace(/:/g, '-')}.txt`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
    };

    /**
     * Switch to a different component
     */
    window.switchLogComponent = function(component) {
        if (component === currentComponent) return;

        // Clear current logs
        clearLogs();
        emptyState.querySelector('p').textContent = 'Switching component...';

        // Een pod hoort bij EEN component, dus een componentwissel gooit de podkeuze weg.
        // Hem laten staan zou een pod van het vorige component meesturen, die de server
        // terecht weigert - een foutmelding op een wissel die de gebruiker gewoon vroeg.
        currentPod = null;
        previousAttempt = false;

        // Send switch command via WebSocket
        if (logSocket && logSocket.readyState === WebSocket.OPEN) {
            logSocket.send(JSON.stringify({
                action: 'switch',
                component: component,
                pod: null,
                previous: false
            }));
            currentComponent = component;
            streamPod = null;
            streamPrevious = false;
            updateStatus('connecting', `Switching to ${component}...`);
        } else {
            // Reconnect with new component
            currentComponent = component;
            connectLogWebSocket();
        }

        // De podlijst hoort bij het NIEUWE component en wordt daarom opnieuw opgehaald.
        loadPods();
    };

    /**
     * Wissel naar een andere pod, of terug naar "Alle pods".
     *
     * Dezelfde weg als de componentwissel: een switch-bericht over de bestaande
     * verbinding, die aan de serverkant het proces netjes afbreekt, de wachtrijen
     * leegmaakt en opnieuw begint. De podnaam wordt daar getoetst tegen de pods van dit
     * component - een geraden naam komt er niet doorheen.
     */
    window.switchLogPod = function(podName) {
        currentPod = podName || null;
        syncPreviousToggle();

        // De vergelijking gaat tegen wat de STROOM draagt en niet tegen wat de kiezer
        // droeg. Anders valt precies de belangrijkste wissel weg: renderPodOptions zet de
        // standaardkeuze zelf in currentPod en stuurt daarna deze change, en op
        // 'nieuw === currentPod' keerde die meteen terug - de kiezer noemde een pod, de
        // verbinding stond nog op de label-selector, en dat was met geen enkele handeling
        // recht te trekken omdat de select al op die waarde stond en dus niets meer vuurt.
        if (currentPod === streamPod && previousAttempt === streamPrevious) return;

        clearLogs();
        emptyState.querySelector('p').textContent = 'Switching pod...';
        sendPodSelection();
    };

    /**
     * Zet de vorige-poging-stand aan of uit en herstart de stroom erop.
     */
    window.toggleLogPrevious = function() {
        if (previousToggle.disabled) return;
        previousAttempt = !!previousToggle.checked;
        if (currentPod === streamPod && previousAttempt === streamPrevious) return;

        clearLogs();
        emptyState.querySelector('p').textContent = previousAttempt
            ? 'Vorige poging ophalen...'
            : 'Verbinden met de logstroom...';
        sendPodSelection();
    };

    /**
     * Stuur de huidige pod- en vorige-poging-stand naar de server.
     */
    function sendPodSelection() {
        if (logSocket && logSocket.readyState === WebSocket.OPEN) {
            streamPod = currentPod;
            streamPrevious = previousAttempt;
            logSocket.send(JSON.stringify({
                action: 'switch',
                component: currentComponent,
                pod: currentPod,
                previous: previousAttempt
            }));
            updateStatus('connecting', currentPod ? `Switching to ${currentPod}...` : 'Switching to alle pods...');
        } else {
            // Nog niet open - dat is de gewone gang bij het openen, want de podlijst kan
            // binnen zijn voordat de verbinding staat. Dan opnieuw verbinden MET de oude
            // socket opgeruimd: een socket die nog aan het verbinden is blijft anders
            // naast de nieuwe doorlopen en levert een tweede stroom in hetzelfde paneel.
            replaceLogWebSocket();
        }
    }

    /**
     * Verbind opnieuw, en laat de vorige verbinding niet als storing achter.
     *
     * De handlers gaan er eerst af: een socket die WIJ vervangen mag geen
     * 'Disconnected'-melding in de statusregel zetten over iets wat de gebruiker niet
     * gevraagd heeft af te breken.
     */
    function replaceLogWebSocket() {
        if (logSocket) {
            logSocket.onopen = null;
            logSocket.onmessage = null;
            logSocket.onclose = null;
            logSocket.onerror = null;
            logSocket.close();
            logSocket = null;
        }
        connectLogWebSocket();
    }

    // Keyboard shortcuts
    document.addEventListener('keydown', function(e) {
        if (!panelOpen) return;

        // HIER STOND DE ESCAPE-AFHANDELING, EN DIE IS WEG OMDAT HIJ NIETS DEED.
        //
        // Eerst wiste Escape de zoekopdracht en sloot hij pas daarna het paneel; dat was
        // eigen toetsafhandeling boven op een eigen invoerveld. Nu is het paneel een
        // <dialog> en is het zoekveld een <nldd-search-field>, en die twee regelen
        // allebei hun eigen kant: de dialog sluit op Escape, en het zoekveld is intern
        // een <input type="search">, waar de browser Escape zelf op afhandelt door de
        // inhoud te wissen - zonder dat de dialog dichtgaat.
        //
        // Gemeten en niet aangenomen: met deze regels erin bleven de twee toetsen in
        // tests/e2e/test_logviewer_gedrag.py groen, en met de regels ERUIT ook. Code die
        // je kunt weghalen zonder dat een meting het merkt, doet niets.

        // Focus search on Ctrl/Cmd + F. Geen .select() erachter: dat bestaat op een
        // <nldd-search-field> niet, en een aanroep die niet bestaat breekt de hele
        // toetsafhandeling.
        if ((e.ctrlKey || e.metaKey) && e.key === 'f') {
            e.preventDefault();
            searchInput.focus();
        }
    });
})();
