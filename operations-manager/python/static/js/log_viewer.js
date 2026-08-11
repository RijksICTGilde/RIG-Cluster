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

    // DOM elements
    const panel = document.getElementById('log-viewer-panel');
    const heading = document.getElementById('log-viewer-heading');
    const componentSelector = document.getElementById('log-component-selector');
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
    window.openLogViewer = function(project, deployment, component, comps) {
        currentProject = project;
        currentDeployment = deployment;
        currentComponent = component;
        components = comps || [];

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
     * Connect to the WebSocket log stream
     */
    function connectLogWebSocket() {
        updateStatus('connecting', 'Connecting...');

        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${protocol}//${window.location.host}/api/logs/stream/${currentProject}?deployment=${encodeURIComponent(currentDeployment)}&component=${encodeURIComponent(currentComponent)}&lines=250`;

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
                    updateStatus('streaming', data.message || 'Streaming logs...');
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
     * Clear search input
     */
    window.clearSearch = function() {
        searchInput.value = '';
        filterLogs();
        searchInput.focus();
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
            updateStatus('streaming', 'Streaming logs...');
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

        // Send switch command via WebSocket
        if (logSocket && logSocket.readyState === WebSocket.OPEN) {
            logSocket.send(JSON.stringify({
                action: 'switch',
                component: component
            }));
            currentComponent = component;
            updateStatus('connecting', `Switching to ${component}...`);
        } else {
            // Reconnect with new component
            currentComponent = component;
            connectLogWebSocket();
        }
    };

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
        // afhandeling - inclusief de Escape hierboven.
        if ((e.ctrlKey || e.metaKey) && e.key === 'f') {
            e.preventDefault();
            searchInput.focus();
        }
    });
})();
