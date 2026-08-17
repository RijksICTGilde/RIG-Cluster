/* De meters van het dashboard: drie halve-cirkelmeters en de netwerkgrafiek.

   Dit is de tekencode die in opi/templates/dashboard.html.j2 als <script>-blok stond,
   ongewijzigd overgenomen - dezelfde straal, dezelfde lijndikte, dezelfde kleurgrenzen
   (85% rood, 70% oranje), dezelfde Chart.js-opties en dezelfde twee reeksen.

   WAAROM HIJ NU IN EEN BESTAND STAAT

   Op de bestaande pagina komen de metrics met de pagina mee, dus kon de code in de pagina
   staan en op DOMContentLoaded draaien. Op de hertekende pagina wordt het blok apart
   opgehaald (hx-get=/dashboard/resource-usage) omdat het dashboard anders op Prometheus
   staat te wachten. De canvassen bestaan dan pas NA dat antwoord, en dus roept het
   fragment deze functies zelf aan zodra het binnen is.

   Chart.js komt van dezelfde CDN als op de bestaande pagina en is een los <script> in de
   pagina. Dat script en het htmx-antwoord komen onafhankelijk van elkaar binnen, dus
   wacht de netwerkgrafiek tot window.Chart er is in plaats van aan te nemen dat het zo
   is. */

function getGaugeColor(percentage) {
    if (percentage >= 85) return '#d52b1e';      /* red */
    if (percentage >= 70) return '#e17000';       /* orange */
    return '#39870c';                              /* green */
}

function initializeGauges(cpuPct, memPct, storagePct) {
    animateGauge('cpu-gauge', cpuPct, getGaugeColor(cpuPct));
    animateGauge('memory-gauge', memPct, getGaugeColor(memPct));
    animateGauge('storage-gauge', storagePct, getGaugeColor(storagePct));
}

function animateGauge(canvasId, targetPct, color) {
    var canvas = document.getElementById(canvasId);
    if (!canvas) return;
    var ctx = canvas.getContext('2d');
    canvas.width = 150;
    canvas.height = 150;
    var centerX = canvas.width / 2;
    var centerY = canvas.height / 2;
    var radius = 60;
    var duration = 800;
    var start = performance.now();

    function draw(now) {
        var progress = Math.min((now - start) / duration, 1);
        var eased = 1 - Math.pow(1 - progress, 3);
        var currentPct = eased * targetPct;

        ctx.clearRect(0, 0, canvas.width, canvas.height);

        /* Background arc */
        ctx.beginPath();
        ctx.arc(centerX, centerY, radius, Math.PI, 2 * Math.PI);
        ctx.strokeStyle = '#f0f0f0';
        ctx.lineWidth = 12;
        ctx.lineCap = 'butt';
        ctx.stroke();

        /* Progress arc */
        if (currentPct > 0) {
            var angle = Math.PI + (Math.PI * currentPct / 100);
            ctx.beginPath();
            ctx.arc(centerX, centerY, radius, Math.PI, angle);
            ctx.strokeStyle = color;
            ctx.lineWidth = 12;
            ctx.lineCap = 'round';
            ctx.stroke();
        }

        /* Center dot */
        ctx.beginPath();
        ctx.arc(centerX, centerY, 10, 0, 2 * Math.PI);
        ctx.fillStyle = color;
        ctx.globalAlpha = 0.15 + 0.85 * eased;
        ctx.fill();
        ctx.globalAlpha = 1;

        if (progress < 1) requestAnimationFrame(draw);
    }
    requestAnimationFrame(draw);
}

function initializeCharts(networkInData, networkOutData) {
    var canvas = document.getElementById('network-chart');
    if (!canvas) return;

    /* Chart.js is een los <script> in de pagina; het htmx-antwoord kan er eerder zijn. */
    if (typeof Chart === 'undefined') {
        if (initializeCharts.tries === undefined) initializeCharts.tries = 0;
        if (initializeCharts.tries > 100) return;
        initializeCharts.tries += 1;
        setTimeout(function() { initializeCharts(networkInData, networkOutData); }, 100);
        return;
    }

    var labels = networkInData.map(function(d) { return d.t; });
    var inValues = networkInData.map(function(d) { return d.v; });
    var outValues = networkOutData.map(function(d) { return d.v; });

    if (labels.length === 0) {
        labels = ['--'];
        inValues = [0];
        outValues = [0];
    }

    var networkCtx = canvas.getContext('2d');
    new Chart(networkCtx, {
        type: 'line',
        data: {
            labels: labels,
            datasets: [{
                label: 'Inbound (KB/s)',
                data: inValues,
                borderColor: '#007bc7',
                backgroundColor: 'rgba(0, 123, 199, 0.1)',
                fill: true,
                tension: 0.4
            }, {
                label: 'Outbound (KB/s)',
                data: outValues,
                borderColor: '#e17000',
                backgroundColor: 'rgba(225, 112, 0, 0.1)',
                fill: true,
                tension: 0.4
            }]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    display: false
                }
            },
            scales: {
                y: {
                    display: false
                },
                x: {
                    display: false
                }
            }
        }
    });
}
