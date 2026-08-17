/* De grafieken van het metingenfragment: een lijn per meting, met de limiet erbij.

   Dit is de tekencode die in opi/templates/project-details.html.j2 als <script>-blok
   stond, ongewijzigd overgenomen - dezelfde .metrics-chart-selector, dezelfde
   data-attributen (timestamps, values, limit, request, warning, critical, color),
   dezelfde annotatielijnen en dezelfde Chart.js-opties.

   WAAROM HIJ NU IN EEN BESTAND STAAT

   Het metingenfragment wordt door TWEE vormgevingen gerenderd: partials/deployment_metrics
   .html.j2 (roos) en templates_lotc/bg/_deployment-metrics.html.j2 (NLDD). Beide zetten
   dezelfde canvassen neer met dezelfde id's, en beide laten ze door deze functie tekenen.
   Een tweede kopie zou uit de pas gaan lopen, precies zoals bij de meters van het dashboard
   (static/js/dashboard_gauges.js).

   De DOMContentLoaded-binding hoort erbij en is meeverhuisd: de canvassen komen via htmx
   binnen en het fragment roept initMetricsCharts() daarna zelf aan, maar op een pagina
   waar ze al bij het laden staan tekent deze binding ze.

   Chart.js en chartjs-plugin-annotation komen van dezelfde CDN als op de bestaande pagina
   en worden door de pagina zelf ingeladen. */

/**
 * Convert Unix timestamps to local time labels (HH:MM format)
 */
function timestampsToLocalLabels(timestamps) {
    return timestamps.map(ts => {
        const date = new Date(ts * 1000);  // Convert seconds to milliseconds
        return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
    });
}

/**
 * Initialize all line charts for metrics with limit annotations
 */
function initMetricsCharts() {
    document.querySelectorAll('.metrics-chart').forEach(canvas => {
        const timestamps = JSON.parse(canvas.dataset.timestamps || '[]');
        const labels = timestampsToLocalLabels(timestamps);
        const values = JSON.parse(canvas.dataset.values || '[]');
        const color = canvas.dataset.color || '#39870c';
        const limitStr = canvas.dataset.limit;
        const limit = (limitStr && limitStr !== 'null' && limitStr !== 'None') ? parseFloat(limitStr) : null;
        const requestStr = canvas.dataset.request;
        const request = (requestStr && requestStr !== 'null' && requestStr !== 'None') ? parseFloat(requestStr) : null;

        // Parse warning and critical thresholds for PVC storage charts
        const warningStr = canvas.dataset.warning;
        const criticalStr = canvas.dataset.critical;
        const warning = (warningStr && warningStr !== 'null' && warningStr !== 'None') ? parseFloat(warningStr) : null;
        const critical = (criticalStr && criticalStr !== 'null' && criticalStr !== 'None') ? parseFloat(criticalStr) : null;
        const isPvcChart = canvas.classList.contains('pvc-storage-chart');

        if (timestamps.length === 0 || values.length === 0) {
            // Draw "no data" message
            const ctx = canvas.getContext('2d');
            ctx.fillStyle = '#999';
            ctx.font = '12px sans-serif';
            ctx.textAlign = 'center';
            ctx.fillText('No data available', canvas.width / 2, canvas.height / 2);
            return;
        }

        // Build annotation config for limit line
        const annotations = {};

        // For PVC storage charts, add warning and critical threshold lines
        if (isPvcChart) {
            if (warning !== null) {
                annotations.warningLine = {
                    type: 'line',
                    yMin: warning,
                    yMax: warning,
                    borderColor: '#e17000',
                    borderWidth: 2,
                    borderDash: [4, 4],
                    label: {
                        display: true,
                        content: '80%',
                        position: 'start',
                        backgroundColor: '#e17000',
                        color: '#fff',
                        font: { size: 9 }
                    }
                };
            }
            if (critical !== null) {
                annotations.criticalLine = {
                    type: 'line',
                    yMin: critical,
                    yMax: critical,
                    borderColor: '#d52b1e',
                    borderWidth: 2,
                    borderDash: [4, 4],
                    label: {
                        display: true,
                        content: '90%',
                        position: 'start',
                        backgroundColor: '#d52b1e',
                        color: '#fff',
                        font: { size: 9 }
                    }
                };
            }
            if (limit !== null) {
                annotations.capacityLine = {
                    type: 'line',
                    yMin: limit,
                    yMax: limit,
                    borderColor: '#154273',
                    borderWidth: 2,
                    borderDash: [6, 4],
                    label: {
                        display: true,
                        content: 'Capacity',
                        position: 'end',
                        backgroundColor: '#154273',
                        color: '#fff',
                        font: { size: 9 }
                    }
                };
            }
        } else {
            if (limit !== null) {
                annotations.limitLine = {
                    type: 'line',
                    yMin: limit,
                    yMax: limit,
                    borderColor: '#d52b1e',
                    borderWidth: 2,
                    borderDash: [6, 4],
                    label: {
                        display: true,
                        content: 'Limit',
                        position: 'end',
                        backgroundColor: '#d52b1e',
                        color: '#fff',
                        font: { size: 10 }
                    }
                };
            }
            // Show the request line only when it differs from the limit
            // (the tuner adjusts requests, so request often sits below limit)
            if (request !== null && request !== limit) {
                annotations.requestLine = {
                    type: 'line',
                    yMin: request,
                    yMax: request,
                    borderColor: '#007bc7',
                    borderWidth: 2,
                    borderDash: [3, 3],
                    label: {
                        display: true,
                        content: 'Request',
                        position: 'start',
                        backgroundColor: '#007bc7',
                        color: '#fff',
                        font: { size: 10 }
                    }
                };
            }
        }

        // Calculate y-axis max to include limit/request if present
        const maxValue = Math.max(...values);
        const refMax = Math.max(limit || 0, request || 0);
        const yMax = refMax ? Math.max(maxValue * 1.1, refMax * 1.1) : maxValue * 1.1;

        new Chart(canvas, {
            type: 'line',
            data: {
                labels: labels,
                datasets: [{
                    data: values,
                    borderColor: color,
                    backgroundColor: color + '20',
                    borderWidth: 2,
                    fill: true,
                    tension: 0.3,
                    pointRadius: 2,
                    pointHoverRadius: 4,
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {
                        display: false
                    },
                    tooltip: {
                        callbacks: {
                            label: function(context) {
                                let label = context.parsed.y.toFixed(isPvcChart ? 2 : 1);
                                if (isPvcChart) {
                                    label += ' GB';
                                }
                                if (limit) {
                                    const pct = ((context.parsed.y / limit) * 100).toFixed(0);
                                    label += ` (${pct}% of ${isPvcChart ? 'capacity' : 'limit'})`;
                                }
                                return label;
                            }
                        }
                    },
                    annotation: {
                        annotations: annotations
                    }
                },
                scales: {
                    x: {
                        display: true,
                        grid: {
                            display: false
                        },
                        ticks: {
                            font: {
                                size: 10
                            },
                            maxRotation: 0
                        }
                    },
                    y: {
                        display: true,
                        beginAtZero: true,
                        max: yMax,
                        grid: {
                            color: '#e5e5e5'
                        },
                        ticks: {
                            font: {
                                size: 10
                            }
                        }
                    }
                }
            }
        });
    });
}

// Initialize charts when DOM is loaded
document.addEventListener('DOMContentLoaded', initMetricsCharts);
