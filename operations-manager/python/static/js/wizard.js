/**
 * Wizard JS - extracted from wizard_page.html.j2 and roos.py
 *
 * Contains: sequence management, KV editor, service card dependencies,
 * rerender handler.
 */

/* ========================================================================
 * Sequence item management
 *
 * Context-aware: works in both the wizard (HTMX form submit) and the
 * detail-page edit modal (fetch to /sequence endpoint).
 * ======================================================================== */

function sequenceAdd(path) {
    _sequenceDispatch('add', path, '');
}

function sequenceRemove(path, index) {
    _sequenceDispatch('remove', path, String(index));
}

/**
 * Route the sequence action to the correct handler based on context.
 */
function _sequenceDispatch(action, path, index) {
    /* MELDEN WAT ER GEBEURT.
     *
     * Deze functie had drie stille uitgangen: geen formulier, verzoek nog bezig, of geen
     * bewerkdialoog. In alle drie gebeurde er niets, zonder fout en zonder verzoek, en dan
     * lijkt de knop stuk terwijl hij keurig is aangeroepen. Dat kostte een middag zoeken.
     * Elke uitgang zegt nu wat hij doet, zodat de console het antwoord geeft in plaats van
     * dat iemand het moet reconstrueren. */
    var form = document.getElementById('wizard-step-form')
            || document.getElementById('modal-wizard-form');
    if (form) {
        /* A [data-rerender] change (e.g. toggling a service) fires its own re-render
           submit on this form. While that request is in flight, htmx drops a second
           submit triggered here, so the sequence action would be silently lost and the
           in-flight re-render would then swap the form out from under it. Wait for the
           in-flight request to settle, then re-dispatch against the fresh form. */
        if (form.classList.contains('htmx-request')) {
            document.body.addEventListener(
                'htmx:afterSettle',
                function () { _sequenceDispatch(action, path, index); },
                { once: true }
            );
            return;
        }
        /* Wizard / modal-wizard context: inject hidden fields and trigger HTMX submit */
        _seqHidden(form, '_seq_action', action);
        _seqHidden(form, '_seq_path', path);
        _seqHidden(form, '_seq_index', index);

        /* GEEN BROWSERVALIDATIE OP EEN RIJ ERBIJ OF ERAF.
         *
         * Dit is geen opslaan maar een herteken-actie: de server krijgt de stap terug met
         * een rij meer of minder. Toch liep hij op de validatie van het formulier stuk.
         *
         * Gemeten in de modal-wizard bij Bijlagen: een nieuwe rij brengt een <select
         * required> mee die leeg begint ("-- Kies een bijlage --"). Vanaf dat moment
         * weigert de browser ELKE indiening van dit formulier, en omdat het veld in een
         * nldd-form-field zit zie je ook de foutbel niet. Het gevolg is dat alle knoppen
         * dood lijken, ook die van andere secties, want het is een formulier. Geen fout,
         * geen verzoek, niets.
         *
         * Bij poorten en paden viel het niet op: daar komt een nieuwe rij met een waarde.
         *
         * noValidate rond de indiening zet dat uit voor deze ene actie. htmx kijkt naar
         * dezelfde vlag, dus dit dekt zijn eigen validatiestap mee. Daarna meteen terug,
         * zodat de knop Volgende wel gewoon valideert. */
        var validatieStond = form.noValidate;
        form.noValidate = true;
        htmx.trigger(form, 'submit');
        form.noValidate = validatieStond;
        return;
    }

    /* Detail-edit modal context (legacy non-wizard edit) */
    var modal = document.getElementById('edit-section-modal');
    if (modal && modal.dataset.projectName && modal.dataset.sectionId) {
        _sequenceEditModal(modal, action, path, index);
        return;
    }
    /* Hier gebeurt er werkelijk niets meer: geen wizardformulier en geen bewerkdialoog.
       Stilte kostte ons een middag zoeken, dus dit ene geval meldt zich wel. */
    console.warn('[sequence] geen wizardformulier en geen bewerkdialoog gevonden; er is niets gebeurd');
}

/**
 * Handle sequence action in the detail-edit modal via fetch.
 */
function _sequenceEditModal(modal, action, path, index) {
    var contentEl = document.getElementById('edit-section-content');
    if (!contentEl) return;
    if (typeof collectFormData !== 'function') return;

    var projectName = modal.dataset.projectName;
    var sectionId = modal.dataset.sectionId;

    var formData = collectFormData(contentEl);
    formData['_seq_action'] = action;
    formData['_seq_path'] = path;
    formData['_seq_index'] = index;

    fetch('/projects/' + projectName + '/edit/' + sectionId + '/sequence', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(formData),
    })
    .then(function(response) {
        if (!response.ok) throw new Error('Fout bij reeks-actie');
        return response.text();
    })
    .then(function(html) {
        // Safe: HTML comes from authenticated OPI endpoint with server-side Jinja2 escaping
        contentEl.innerHTML = html;
        /* Re-init service cards if present */
        contentEl.querySelectorAll('.service-cards-grid').forEach(function(grid) {
            delete grid.dataset.initialized;
            initServiceCards(grid);
        });
        if (typeof initKvEditors === 'function') initKvEditors(contentEl);
    })
    .catch(function(err) {
        var errorEl = document.getElementById('edit-section-error');
        if (errorEl) {
            errorEl.textContent = err.message;
            // De verborgen begintoestand staat als .is-hidden in wizard.css, zodat de
            // markup van de foutmelding geen vormgeving hoeft te dragen.
            errorEl.classList.remove('is-hidden');
        }
    });
}

function _seqHidden(form, name, value) {
    var el = form.querySelector('input[name="' + name + '"]');
    if (!el) {
        el = document.createElement('input');
        el.type = 'hidden';
        el.name = name;
        form.appendChild(el);
    }
    el.value = value;
}

/* ========================================================================
 * Key-value editor: toggle between ENV and YAML format
 * ======================================================================== */

function kvToggleFormat(editorId, newFormat) {
    var editor = document.getElementById(editorId);
    if (!editor) return;
    var oldFormat = editor.dataset.format;
    if (oldFormat === newFormat) return;

    var textarea = editor.querySelector('textarea');
    if (!textarea) return;

    var lines = textarea.value.split('\n');
    var converted = [];
    for (var i = 0; i < lines.length; i++) {
        var line = lines[i].trim();
        if (!line || line.charAt(0) === '#') { converted.push(lines[i]); continue; }
        var key, val;
        if (oldFormat === 'env' && line.indexOf('=') !== -1) {
            var eqIdx = line.indexOf('=');
            key = line.substring(0, eqIdx).trim();
            val = line.substring(eqIdx + 1).trim();
        } else if (oldFormat === 'yaml' && line.indexOf(': ') !== -1) {
            var colIdx = line.indexOf(': ');
            key = line.substring(0, colIdx).trim();
            val = line.substring(colIdx + 2).trim();
        } else {
            converted.push(lines[i]); continue;
        }
        converted.push(newFormat === 'env' ? key + '=' + val : key + ': ' + val);
    }
    textarea.value = converted.join('\n');
    editor.dataset.format = newFormat;

    var btns = editor.querySelectorAll('.kv-toggle__btn');
    for (var b = 0; b < btns.length; b++) {
        btns[b].className = btns[b].dataset.format === newFormat
            ? 'kv-toggle__btn kv-toggle__btn--active'
            : 'kv-toggle__btn';
    }
    if (typeof switchKvLanguage === 'function') switchKvLanguage(editorId, newFormat);
}

/* ========================================================================
 * Service card dependency logic (client-side)
 *
 * Implements the SAME algorithm as the server-side Python:
 *   1. requiresMap  - from data-requires attributes
 *   2. reverseDeps  - dep -> [active services that need it]
 *   3. locked       - checked AND reverseDeps[svc].length > 0
 *
 * IMPORTANT: After process_components, ROOS c-checkbox renders as:
 *   <label class="rvo-checkbox" data-roos-component="checkbox">
 *     <input type="checkbox" class="rvo-checkbox__input" ...>
 *   </label>
 * So we work with native <input type="checkbox"> elements.
 * ======================================================================== */

function initServiceCards(grid) {
    if (!grid || grid.dataset.initialized) return;

    /*
     * ALLEEN EEN RASTER MET ECHTE DIENSTKAARTEN.
     *
     * De VOORINSTELLINGEN ("Snelstart: kies een scenario") gebruiken dezelfde klassen -
     * .service-cards-grid en .service-card - want ze zien eruit als dienstkaarten. Maar ze
     * dragen geen data-service: een voorinstelling is geen dienst, hij stuurt bij een klik
     * zijn eigen hx-post en heeft geen afhankelijkheden en geen slot.
     *
     * Zonder deze poort liep dat raster toch door de dienstenlogica heen. Dat was lang
     * onzichtbaar, want de melding bij een vergrendelde kaart werd uit een regel IN de kaart
     * gelezen en die stond er niet. Sinds de melding zijn tekst zelf opbouwt kwam er wel
     * iets: "undefined is vereist en kan niet worden uitgezet", op een voorinstelling
     * aanklikken. Gemeld op de keycloak-configuratiestap.
     */
    if (!grid.querySelector('.service-card[data-service]')) return;

    grid.dataset.initialized = 'true';

    var processing = false;

    /* De dialoog die uitlegt waarom een dienst vastzit. Hij staat naast het raster in
       widgets/service_cards.html.j2, met een id afgeleid van dezelfde veldnaam. */
    var melding = document.getElementById(String(grid.id || '').replace(/-grid$/, '-melding'));

    /*
     * ZEGGEN WAAROM, IN DE DIALOOG VAN HET THEMA.
     *
     * Hier stond window.alert(). Dat is een systeemvenster: het draagt de naam van de host,
     * het weet niets van de vormgeving, en het bevriest de hele pagina tot je klikt. Zo ook
     * gemeld. <nldd-modal-dialog> draait op een echte <dialog> met showModal(), dus de
     * toplaag, de focus en Escape komen van de browser, en hij ziet eruit als de rest.
     *
     * Valt terug op alert() als de dialoog er niet is: dan is er iets mis met het sjabloon,
     * en dan is een lelijke melding beter dan een klik die stil niets doet.
     */
    function zegWaarom(tekst) {
        /* Een dialoog van deze lijst, en anders de eerste op de pagina: een melding hoort
           er overal hetzelfde uit te zien, en nooit een systeemvenster te worden omdat een
           sjabloon zijn eigen dialoog niet meebracht. */
        var venster = melding || document.querySelector('nldd-modal-dialog');
        if (!venster || typeof venster.show !== 'function') {
            alert(tekst);
            return;
        }
        melding = venster;
        melding.setAttribute('text', 'Deze dienst is vereist');
        melding.setAttribute('supporting-text', tekst);
        melding.show();
    }

    /* De reden dat een dienst vastzit, in woorden. Stond als regel IN de kaart; daar liet
       hij bij het verschijnen alle aanvinkvakjes van de rij verspringen. */
    function slotReden(svc) {
        var requirers = getReverseDeps()[svc] || [];
        var namen = requirers.map(function (r) { return getLabel(r); });
        if (!namen.length) return getLabel(svc) + ' is vereist en kan niet worden uitgezet.';
        return getLabel(svc) + ' is vereist door ' + namen.join(', ') + ' en kan niet worden uitgezet.';
    }

    /* step 1: build requiresMap from data-requires attrs */
    var requiresMap = {};
    var allServices = [];
    grid.querySelectorAll('.service-card').forEach(function(card) {
        var svc = card.dataset.service;
        allServices.push(svc);
        var req = card.dataset.requires;
        if (req) {
            try { requiresMap[svc] = JSON.parse(req); } catch(e) {}
        }
    });

    function getCard(svc) {
        return grid.querySelector('[data-service="' + svc + '"]');
    }
    /*
     * HET BESTURINGSELEMENT IS HET COMPONENT, NIET EEN <input>.
     *
     * Hier stond querySelector('input[type="checkbox"]'), en dat vond de kale <input> die
     * de kaart vroeger zelf meebracht. De kaart is nu een <c-card> met een <c-checkbox>,
     * en dat wordt onder NLDD een <nldd-checkbox-field>: form-associated, met zijn echte
     * <input> twee schaduwbomen diep. Een selector op input vindt daar niets - dat is
     * precies waar de vorige poging op strandde.
     *
     * Het element zelf draagt .checked als eigenschap: lezen en zetten werkt er gewoon op,
     * en het stuurt zijn waarde mee via ElementInternals (voor htmx rechtgezet in
     * static/js/form-associated.js). Zie features/aanvinkvakje.md.
     */
    var VAKJE = 'nldd-checkbox-field, nldd-checkbox, input[type="checkbox"]';
    function getCheckbox(svc) {
        var card = getCard(svc);
        return card ? card.querySelector(VAKJE) : null;
    }
    function isChecked(svc) {
        var cb = getCheckbox(svc);
        return cb ? !!cb.checked : false;
    }
    /**
     * Een geweigerde klik ONGEDAAN MAKEN, EEN TIK LATER.
     *
     * Hier stond een herstel binnen dezelfde gebeurtenis, en dat leverde een stille desync
     * op. Gemeten in de browser, stap voor stap, met de schaduwboom erbij:
     *
     *     na de geweigerde klik : host checked=true,  eigen <input> checked=false
     *     volgende klik         : <input> gaat naar true, host blijft true -> er gebeurt NIETS
     *     de klik daarna        : nu weer gelijk, en pas dan werkt uitvinken
     *
     * Zo voelde het ook: een vergrendelde dienst leek uit te kunnen, en daarna reageerde het
     * vakje een klik lang niet.
     *
     * Waar het vandaan komt: bij een klik zet het component zijn eigen `checked` op false en
     * plant het een hertekening. Zetten wij `checked` in diezelfde gebeurtenis terug op true,
     * dan ziet die hertekening dezelfde waarde als de vorige keer en schrijft hij niets -
     * terwijl de browser het vakje in de schaduwboom al had uitgezet.
     *
     * Een tik later is de hertekening geweest en is false->true wel een echte wijziging, dus
     * schrijft het component zijn eigen vakje bij. `processing` blijft tot dan aanstaan, zodat
     * de change die daaruit volgt niet opnieuw door de handler loopt.
     *
     * Dit patroon komt terug bij elk component dat zijn besturingselement in een schaduwboom
     * tekent. Het staat uitgeschreven in features/aanvinkvakje.md, onder "Het vakje vanuit
     * eigen JavaScript aansturen": welke drie standen het eens moeten zijn, wat wel en niet
     * werkt, en hoe je het meet.
     */
    function herstelVakje(svc, klaar) {
        setTimeout(function () {
            setChecked(svc, true);
            updateAllVisuals();
            klaar();
        }, 0);
    }

    /** De stand zetten op het vakje, plus het gekozen vlak van de kaart eromheen. */
    function setChecked(svc, checked) {
        var cb = getCheckbox(svc);
        if (cb) cb.checked = checked;
        syncKaart(svc, checked);
    }
    /* Het GEKOZEN VLAK is het background-attribuut van <nldd-card>, geen eigen klasse met
       een eigen kleur: de kaart tekent zichzelf, wij zeggen alleen welke stand hij heeft. */
    function syncKaart(svc, checked) {
        var card = getCard(svc);
        if (card) card.setAttribute('background', checked ? 'tinted' : 'base');
    }
    /* De naam staat op de kaart in data-label en niet op het aanvinkvakje: dat draagt
       "Gebruiken" als label, want de naam staat al als kop in de kaart. */
    function getLabel(svc) {
        var card = getCard(svc);
        return (card && card.dataset.label) || svc;
    }

    /* step 2: reverse map (recomputed on every change) */
    function getReverseDeps() {
        var rev = {};
        for (var svc in requiresMap) {
            if (!isChecked(svc)) continue;
            requiresMap[svc].forEach(function(dep) {
                if (!rev[dep]) rev[dep] = [];
                rev[dep].push(svc);
            });
        }
        return rev;
    }

    /* transitive dependencies for auto-select */
    function getTransitiveDeps(svc, visited) {
        visited = visited || {};
        if (visited[svc]) return [];
        visited[svc] = true;
        var deps = requiresMap[svc] || [];
        var all = deps.slice();
        deps.forEach(function(dep) {
            getTransitiveDeps(dep, visited).forEach(function(d) {
                if (all.indexOf(d) === -1) all.push(d);
            });
        });
        return all;
    }

    /* step 3: update locked / selected / hint for all cards */
    function updateAllVisuals() {
        var revDeps = getReverseDeps();
        allServices.forEach(function(svc) {
            var card = getCard(svc);
            if (!card) return;
            var cb = getCheckbox(svc);
            var checked = cb ? cb.checked : false;

            card.classList.toggle('service-card--selected', checked);
            syncKaart(svc, checked);

            var requirers = revDeps[svc] || [];
            var serverLocked = card.dataset.locked === 'true';
            var locked = checked && (requirers.length > 0 || serverLocked);

            card.classList.toggle('service-card--locked-checked', locked);

            /* Bewust GEEN disabled op het verborgen vakje. Vergrendeld betekent "niet
               aanpasbaar", en disabled betekent daarnaast "niet versturen" -- dat tweede
               bedoelen we niet, en juist daardoor viel een vergrendelde dienst uit de POST.
               Het slot wordt bewaakt door de change-handler hieronder (die de wijziging
               terugdraait) en door de server. aria-disabled staat op de RIJ, want dat is
               het element met role="checkbox" dat een schermlezer voorleest. */
            card.setAttribute('aria-disabled', locked ? 'true' : 'false');

            /* GEEN REGEL MEER IN DE KAART. Die verscheen en verdween met het slot, en
               omdat de kaarten van een rij even hoog zijn en het vakje aan de onderrand
               hangt, sprongen alle vakjes van die rij mee. De reden wordt nu verteld op
               het moment dat je hem nodig hebt, in zegWaarom(). */
        });
    }

    /* handle a service being toggled */
    function handleToggle(svc) {
        if (isChecked(svc)) {
            /* auto-select transitive dependencies */
            getTransitiveDeps(svc).forEach(function(dep) {
                if (!isChecked(dep)) {
                    setChecked(dep, true);
                }
            });
        }
        updateAllVisuals();
    }

    /*
     * DE RIJ MELDT ZIJN EIGEN WIJZIGING.
     *
     * <nldd-list-item checkbox> handelt de klik op de hele rij en de toetsenbordbediening
     * zelf af, zet daarna zijn eigen `checked` en stuurt een change-gebeurtenis omhoog.
     * Er is hier dus geen eigen klikafhandeling meer nodig - die stond er alleen omdat de
     * kaart eromheen onze eigen <div> was en zelf niets kon.
     *
     * Wat wij nog doen is de stand overnemen in het verborgen aanvinkvakje (de waarde die
     * het formulier verstuurt), het slot bewaken, en de afhankelijkheden bijwerken.
     */
    /*
     * HET VAKJE MELDT ZIJN EIGEN WIJZIGING.
     *
     * <nldd-checkbox-field> handelt de klik en de toetsenbordbediening zelf af en stuurt
     * daarna een change omhoog. Er is hier geen eigen klikafhandeling meer: die stond er
     * alleen omdat de kaart eromheen onze eigen <div> was en zelf niets kon. Wat wij nog
     * doen is het slot bewaken en de afhankelijkheden bijwerken.
     */
    grid.addEventListener('change', function(e) {
        if (processing) return;
        var card = e.target.closest ? e.target.closest('.service-card') : null;
        if (!card) return;
        var svc = card.dataset.service;
        var aan = isChecked(svc);

        /* Hier is het slot een slot. Het vakje is niet disabled -- anders zou het zijn
           waarde niet versturen -- dus wordt het uitvinken teruggedraaid en wordt gezegd
           waarom. */
        if (card.classList.contains('service-card--locked-checked') && !aan) {
            processing = true;
            herstelVakje(svc, function () { processing = false; });
            zegWaarom(slotReden(svc));
            return;
        }

        processing = true;
        syncKaart(svc, aan);
        handleToggle(svc);
        processing = false;
    });

    /*
     * DE HELE KAART IS EEN KLIKDOEL.
     *
     * Het aanvinkvakje bedient zichzelf, maar een kaart van 300 bij 120 waarvan alleen het
     * vakje links reageert is een klein doel met veel dood vlak eromheen. Deze handler
     * maakt de rest van de kaart een tweede ingang naar hetzelfde vakje.
     *
     * Programmatisch .checked zetten stuurt GEEN change-gebeurtenis (dat is zo voor een
     * gewone <input> en ook voor dit component), dus de afhankelijkheden worden hier zelf
     * bijgewerkt in plaats van via de handler hierboven.
     */
    grid.querySelectorAll('.service-card').forEach(function(card) {
        card.addEventListener('click', function(e) {
            /* Het vakje en het vraagteken handelen hun eigen klik af. Zonder deze uitzondering
               zet de kaart het vakje meteen weer terug. */
            if (e.target.closest('nldd-checkbox-field, nldd-checkbox, .service-card__help-btn')) return;
            if (processing) return;

            var svc = card.dataset.service;
            if (card.classList.contains('service-card--locked-checked')) {
                zegWaarom(slotReden(svc));
                return;
            }

            var cb = getCheckbox(svc);
            if (!cb || cb.disabled) return;
            processing = true;
            setChecked(svc, !cb.checked);
            handleToggle(svc);
            processing = false;
        });
    });
}

/* ========================================================================
 * Service help modal
 * ======================================================================== */

var focusVoorHulp = null;

function openServiceHelp(templateName) {
    var backdrop = document.getElementById('service-help-backdrop');
    var modal = document.getElementById('service-help-modal');
    var content = document.getElementById('service-help-content');
    if (!backdrop || !modal || !content) return;

    content.innerHTML = '<p>Laden...</p>';
    backdrop.classList.add('is-open');
    modal.classList.add('is-open');
    document.body.style.overflow = 'hidden';
    /* De focus MOET de dialoog in. Page Up/Page Down scrollen het scrollgebied van het
       element dat focus heeft; die bleef op de vraagtekenknop staan, dus scrollde de
       pagina eronder en niet deze dialoog. Zie ook static/js/edit_modal.js. */
    focusVoorHulp = document.activeElement;
    modal.focus();

    /* A service's help text is addressed as "<service-package>/help.md", so
       encode per path segment -- encodeURIComponent would escape the separator. */
    var encoded = templateName.split('/').map(encodeURIComponent).join('/');
    fetch('/forms/wizard/help/' + encoded)
        .then(function(resp) {
            if (!resp.ok) throw new Error('Not found');
            return resp.text();
        })
        .then(function(html) {
            // Safe: HTML comes from authenticated OPI endpoint with server-side Jinja2 escaping
            content.innerHTML = html;
            /* Process ROOS components if the extension is available */
            if (typeof htmx !== 'undefined') {
                htmx.process(content);
            }
        })
        .catch(function() {
            content.innerHTML = '<p>Help-informatie kon niet geladen worden.</p>';
        });
}

function closeServiceHelp() {
    var backdrop = document.getElementById('service-help-backdrop');
    var modal = document.getElementById('service-help-modal');
    if (backdrop) backdrop.classList.remove('is-open');
    if (modal) modal.classList.remove('is-open');
    document.body.style.overflow = '';
    if (focusVoorHulp && typeof focusVoorHulp.focus === 'function') {
        focusVoorHulp.focus();
    }
    focusVoorHulp = null;
}

document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
        var modal = document.getElementById('service-help-modal');
        if (modal && modal.classList.contains('is-open')) {
            closeServiceHelp();
        }
    }
});

/* NAAR DE EERSTE FOUT: die staat niet meer hier.
 *
 * Er stond een scrollToFirstError() die na elke swap naar de eerste fout sprong, en
 * static/js/htmx-formgedrag.js zette na diezelfde swap de cursor terug waar hij was. Twee
 * luisteraars op een gebeurtenis die allebei de focus zetten, waarbij de laatst
 * geregistreerde won. Op een stap met een openstaande fout werd je daardoor bij elke swap
 * uit je veld getrokken.
 *
 * Waar de focus na een swap heen gaat is EEN beslissing, en die staat nu op een plek: in
 * htmx-formgedrag.js, bij het herstel dat er al was. Zie de toelichting daar. */

/* ========================================================================
 * Initialization: on page load and after HTMX swaps
 * ======================================================================== */

function initWizardWidgets(container) {
    container = container || document;
    container.querySelectorAll('.service-cards-grid').forEach(initServiceCards);
    if (typeof initKvEditors === 'function') initKvEditors(container);
}

document.addEventListener('DOMContentLoaded', function() {
    initWizardWidgets();
});

document.addEventListener('htmx:afterSettle', function(event) {
    initWizardWidgets(event.detail.target);

    /* Clean up the _rerender hidden field after a re-render swap completes,
       so the next regular form submit is not treated as another re-render. */
    var form = document.getElementById('wizard-step-form')
            || document.getElementById('modal-wizard-form');
    if (form) {
        var rr = form.querySelector('input[name="_rerender"]');
        if (rr) rr.remove();
    }
});


/* ========================================================================
 * Paste cleaner for container image fields
 *
 * Strips "docker pull " and similar prefixes when pasting into fields
 * marked with data-paste-clean="container-image".
 * ======================================================================== */

document.addEventListener('paste', function(e) {
    var el = e.target.closest('[data-paste-clean="container-image"]')
          || (e.target.getRootNode && e.target.getRootNode().host
              && e.target.getRootNode().host.closest
              && e.target.getRootNode().host.closest('[data-paste-clean="container-image"]'));
    if (!el) return;

    var pasted = (e.clipboardData || window.clipboardData).getData('text');
    if (!pasted) return;

    var cleaned = pasted.trim().replace(/^docker\s+pull\s+/i, '');
    if (cleaned !== pasted) {
        e.preventDefault();
        var input = e.target;
        input.value = cleaned;
        input.dispatchEvent(new Event('input', { bubbles: true }));
        flashCleanIndicator(input);
    }
}, true);

function flashCleanIndicator(input) {
    input.style.transition = 'none';
    input.style.outlineStyle = 'solid';
    input.style.outlineWidth = '3px';
    input.style.outlineColor = '#66bb6a';
    // Force reflow so the bright green is applied before the transition starts
    input.offsetHeight;
    input.style.transition = 'outline-color 0.8s ease-out';
    input.style.outlineColor = '#1b5e20';
    input.addEventListener('transitionend', function cleanup() {
        input.removeEventListener('transitionend', cleanup);
        input.style.outline = '';
        input.style.transition = '';
    });
}

/* ========================================================================
 * Hertekenen na een keuze in een cascade
 * ======================================================================== */

function _huidigStapFormulier() {
    return document.getElementById('wizard-step-form')
        || document.getElementById('modal-wizard-form');
}

function _hertekenNu(form) {
    _seqHidden(form, '_rerender', '1');
    /* Zelfde reden als bij een rij toevoegen of verwijderen: dit is HERTEKENEN en geen
       opslaan. Een cascade vult juist de velden waar de volgende keuze van afhangt, dus op
       dat moment staan er per definitie verplichte velden leeg. Zonder dit weigert de
       browser de indiening zonder een woord te zeggen: geen fout, geen verzoek, en dan
       lijkt het alsof de lijst eronder niet reageert.
       Gemeld op Bron-project bij cross-domain: kiezen leverde geen enkele POST op. */
    var validatieStond = form.noValidate;
    form.noValidate = true;
    htmx.trigger(form, 'submit');
    form.noValidate = validatieStond;
}

/* De BESTURING met deze naam, niet de omhulling eromheen.
 * Een keuzelijst is onder het thema een kale <select> met de naam erop, maar een tekstveld
 * is een <nldd-text-field> met de echte <input> in zijn schaduwboom. Het element dat de
 * waarde draagt is in beide gevallen het eerste met deze naam dat een string-value heeft. */
function _besturingMetNaam(naam) {
    var kandidaten = document.querySelectorAll('[name="' + naam + '"]');
    for (var i = 0; i < kandidaten.length; i++) {
        if (typeof kandidaten[i].value === 'string') return kandidaten[i];
    }
    return null;
}

function _kanDeWaardeDragen(el, waarde) {
    if (!el.options) return true;
    for (var i = 0; i < el.options.length; i++) {
        if (el.options[i].value === waarde) return true;
    }
    return false;
}

/* EEN KEUZE DIE VALT TERWIJL ER NOG EEN VERZOEK LOOPT, MAG NIET VERDWIJNEN.
 *
 * Gemeten in de browser (RC-127), op de cross-domain-stap in de aanmaakwizard: verandert er
 * een tweede [data-rerender]-veld terwijl het formulier al een hertekenverzoek open heeft
 * staan, dan levert die tweede wijziging GEEN htmx:configRequest en GEEN
 * htmx:beforeRequest op - er vertrekt niets, ook niet later:
 *
 *     change  to/component   inflight=false  -> configRequest, beforeRequest
 *     change  from/project    inflight=true  -> (niets)
 *     beforeSwap, afterRequest, afterSettle
 *     de keuzelijst 'from/deployment' biedt daarna alleen [''], en blijft dat
 *
 * htmx zet dat tweede verzoek in de wachtrij van het ELEMENT dat het doet (hier het
 * formulier) en speelt het na het eerste antwoord opnieuw af. Maar het antwoord vervangt
 * #wizard-step-content, en het formulier zit daarbinnen: het haalt zichzelf dus uit de
 * pagina. htmx weigert een verzoek op een element dat niet meer in het document staat, en
 * daarmee is de keuze weg zonder fout, zonder melding en zonder herstel. Dat is precies het
 * beeld van de gestrande cascade: een geldige keuze in de rij en een lege lijst eronder.
 *
 * Vandaar dezelfde bescherming die _sequenceDispatch al had, plus wat daar niet nodig was:
 * de keuze zelf terugzetten. Het antwoord dat onderweg was, is gerenderd ZONDER deze keuze,
 * dus de verse rij komt leeg terug - opnieuw indienen alleen zou een leeg veld versturen.
 */
function _hertekenNaDeSwap(naam, waarde, bron) {
    function haak() {
        var form = _huidigStapFormulier();
        // Nog bezig: het oude formulier draagt htmx-request tot na de swap. Zo landen we
        // niet op de OOB-swap van de stapbalk, die vóór de inhoud kan komen.
        if (!form || form.classList.contains('htmx-request')) return;
        document.body.removeEventListener('htmx:afterSettle', haak);

        if (document.contains(bron)) {
            // De swap raakte ons veld niet, dus de waarde staat er nog: gewoon hertekenen.
            _hertekenNu(form);
            return;
        }
        var vers = naam ? _besturingMetNaam(naam) : null;
        if (!vers) {
            console.warn('[herteken] veld "' + naam + '" is na de swap verdwenen; de keuze is niet doorgegeven');
            return;
        }
        if (vers.value === waarde) return;  // de render die landde kende de keuze al
        if (!_kanDeWaardeDragen(vers, waarde)) {
            console.warn('[herteken] "' + waarde + '" staat niet meer in de lijst van "' + naam + '"');
            return;
        }
        vers.value = waarde;
        vers.dispatchEvent(new Event('change', { bubbles: true }));
    }
    document.body.addEventListener('htmx:afterSettle', haak);
}

/* Re-render the current step when a [data-rerender] field changes */
document.addEventListener('change', function(e) {
    var el = e.target.closest('[data-rerender]');
    if (!el) return;
    var form = _huidigStapFormulier();
    if (!form) return;
    if (form.classList.contains('htmx-request')) {
        _hertekenNaDeSwap(e.target.getAttribute('name'), e.target.value, e.target);
        return;
    }
    _hertekenNu(form);
});
