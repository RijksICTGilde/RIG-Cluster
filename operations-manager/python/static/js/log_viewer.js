/**
 * De logviewer: het schuifpaneel met de live logstroom van een component.
 *
 * Deze code stond in opi/templates/project-details.html.j2 en is daar weggehaald toen de
 * nieuwe vormgeving dezelfde viewer moest kunnen openen. Ze hoort niet bij een pagina
 * maar bij het paneel: beide vormgevingen zetten dezelfde markup neer (dezelfde id's -
 * daar zoekt deze code zijn elementen mee op) en laden dit bestand.
 *
 * LET OP: dit script pakt zijn elementen op het moment dat het draait, niet later. Het
 * moet dus NA de markup van het paneel geladen worden, onderaan de pagina. Laad je het in
 * de <head>, dan zijn alle verwijzingen null en doet de viewer stilzwijgend niets.
 *
 * De inhoud is ongewijzigd overgenomen.
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
    const backdrop = document.getElementById('log-viewer-backdrop');
    const panel = document.getElementById('log-viewer-panel');
    const heading = document.getElementById('log-viewer-heading');
    const deploymentLabel = document.getElementById('log-viewer-deployment');
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
    const searchClear = document.getElementById('log-search-clear');
    const filterError = document.getElementById('filter-error');
    const filterWarn = document.getElementById('filter-warn');
    const filterInfo = document.getElementById('filter-info');
    const filterDebug = document.getElementById('filter-debug');
    const wordWrapToggle = document.getElementById('log-word-wrap');

    /**
     * Open the log viewer panel
     */
    window.openLogViewer = function(project, deployment, component, comps) {
        currentProject = project;
        currentDeployment = deployment;
        currentComponent = component;
        components = comps || [];

        // Update UI
        heading.textContent = `Logs - ${deployment}`;
        deploymentLabel.textContent = deployment;

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

        // Clear previous logs
        clearLogs();

        // Show panel
        backdrop.classList.add('is-open');
        panel.classList.add('is-open');
        document.body.style.overflow = 'hidden';

        // Connect WebSocket
        connectLogWebSocket();
    };

    /**
     * Close the log viewer panel
     */
    window.closeLogViewer = function() {
        // Disconnect WebSocket
        if (logSocket) {
            logSocket.close();
            logSocket = null;
        }

        // Hide panel
        backdrop.classList.remove('is-open');
        panel.classList.remove('is-open');
        document.body.style.overflow = '';

        // Reset state
        logPaused = false;
        pauseBtn.classList.remove('is-active');
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

        // Check level filters
        if (level === 'error' && !filterError.checked) return false;
        if (level === 'warn' && !filterWarn.checked) return false;
        if (level === 'info' && !filterInfo.checked) return false;
        if (level === 'debug' && !filterDebug.checked) return false;

        // Check search filter
        const searchTerm = searchInput.value.toLowerCase().trim();
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
        const searchTerm = searchInput.value.trim();
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
        const searchTerm = searchInput.value.trim();
        const logLineEls = content.querySelectorAll('.log-line');
        let matchCount = 0;

        // Show/hide clear button
        if (searchTerm) {
            searchClear.classList.add('is-visible');
        } else {
            searchClear.classList.remove('is-visible');
        }

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

        if (logPaused) {
            pauseBtn.classList.add('is-active');
            updateStatus('paused', 'Paused');
        } else {
            pauseBtn.classList.remove('is-active');
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
        searchClear.classList.remove('is-visible');
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
        if (!panel.classList.contains('is-open')) return;

        // Close on Escape (or clear search if search is active)
        if (e.key === 'Escape') {
            if (document.activeElement === searchInput && searchInput.value) {
                clearSearch();
            } else {
                closeLogViewer();
            }
        }

        // Focus search on Ctrl/Cmd + F
        if ((e.ctrlKey || e.metaKey) && e.key === 'f') {
            e.preventDefault();
            searchInput.focus();
            searchInput.select();
        }
    });
})();
