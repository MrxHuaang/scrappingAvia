/**
 * Zephr — Frontend Application
 * Liquid glass redesign, SSE streaming, ZIP with real PDFs,
 * date formatting, copy AD#, recent search preview.
 */
(function () {
    'use strict';

    const DEFAULT_TITLE = 'Zephr \u00b7 Airworthiness Directives';
    document.title = DEFAULT_TITLE;

    // ================================================================
    // ATA CHAPTERS
    // ================================================================
    const ATA_CHAPTERS = [
        { code: '05', name: 'Límites de tiempo / Verificaciones de mantenimiento' },
        { code: '06', name: 'Dimensiones y áreas' },
        { code: '21', name: 'Aire acondicionado y presurización' },
        { code: '22', name: 'Piloto automático' },
        { code: '23', name: 'Comunicaciones' },
        { code: '24', name: 'Energía eléctrica' },
        { code: '25', name: 'Equipamiento y mobiliario' },
        { code: '26', name: 'Protección contra incendios' },
        { code: '27', name: 'Controles de vuelo' },
        { code: '28', name: 'Combustible' },
        { code: '29', name: 'Potencia hidráulica' },
        { code: '30', name: 'Protección contra hielo y lluvia' },
        { code: '31', name: 'Instrumentos' },
        { code: '32', name: 'Tren de aterrizaje' },
        { code: '33', name: 'Luces' },
        { code: '34', name: 'Navegación' },
        { code: '35', name: 'Oxígeno' },
        { code: '36', name: 'Sistema neumático' },
        { code: '38', name: 'Agua y residuos' },
        { code: '49', name: 'Unidad de potencia auxiliar (APU)' },
        { code: '51', name: 'Estructuras estándar' },
        { code: '52', name: 'Puertas' },
        { code: '53', name: 'Fuselaje' },
        { code: '54', name: 'Góndolas y pilones' },
        { code: '55', name: 'Estabilizadores' },
        { code: '56', name: 'Ventanas' },
        { code: '57', name: 'Alas' },
        { code: '61', name: 'Hélices' },
        { code: '71', name: 'Planta motriz' },
        { code: '72', name: 'Motor (turbina/turbohélice)' },
        { code: '73', name: 'Control de combustible del motor' },
        { code: '74', name: 'Ignición del motor' },
        { code: '75', name: 'Purga del motor' },
        { code: '76', name: 'Controles del motor' },
        { code: '77', name: 'Indicadores del motor' },
        { code: '78', name: 'Escape del motor' },
        { code: '79', name: 'Aceite del motor' },
        { code: '80', name: 'Arranque del motor' },
    ];

    const ATA_KEYWORDS = {
        '05': ['time limit', 'maintenance check', 'inspection interval', 'life limit'],
        '21': ['air conditioning', 'pressurization', 'cabin pressure', 'pack', 'aire acondicionado'],
        '22': ['autopilot', 'auto pilot', 'flight director', 'piloto automático'],
        '23': ['communication', 'radio', 'vhf', 'hf', 'satcom', 'comunicación'],
        '24': ['electrical', 'generator', 'battery', 'wiring', 'eléctric'],
        '25': ['equipment', 'furnishing', 'seat', 'cargo', 'equipamiento'],
        '26': ['fire protection', 'fire extinguish', 'smoke detect', 'incendio'],
        '27': ['flight control', 'aileron', 'elevator', 'rudder', 'flap', 'slat', 'spoiler', 'control de vuelo'],
        '28': ['fuel', 'combustible', 'tank', 'tanque', 'fuel pump', 'bomba'],
        '29': ['hydraulic', 'hidráulic'],
        '30': ['ice protection', 'rain', 'de-ice', 'anti-ice', 'hielo', 'lluvia'],
        '31': ['instrument', 'instrumento', 'altimeter', 'airspeed'],
        '32': ['landing gear', 'gear', 'wheel', 'brake', 'tire', 'strut', 'nose gear', 'main gear', 'tren de aterrizaje'],
        '33': ['light', 'lamp', 'beacon', 'strobe', 'luces', 'luz'],
        '34': ['navigation', 'gps', 'vor', 'ils', 'navegación', 'fms'],
        '35': ['oxygen', 'oxígeno', 'o2'],
        '36': ['pneumatic', 'bleed', 'neumático'],
        '38': ['water', 'waste', 'agua', 'residuo', 'lavatory'],
        '49': ['apu', 'auxiliary power', 'potencia auxiliar'],
        '51': ['structure', 'estructura', 'fuselage frame', 'skin panel'],
        '52': ['door', 'puerta', 'hatch', 'escape'],
        '53': ['fuselage', 'fuselaje', 'belly', 'skin'],
        '54': ['nacelle', 'pylon', 'góndola', 'pilón'],
        '55': ['stabilizer', 'estabilizador', 'horizontal stab', 'vertical stab', 'tail'],
        '56': ['window', 'windshield', 'ventana', 'parabrisas'],
        '57': ['wing', 'ala', 'spar', 'rib', 'winglet'],
        '61': ['propeller', 'hélice', 'prop'],
        '71': ['powerplant', 'planta motriz', 'engine mount', 'cowl'],
        '72': ['engine', 'motor', 'turbine', 'turbina', 'compressor', 'fan blade'],
        '73': ['fuel control', 'fuel governor', 'control de combustible'],
        '74': ['ignition', 'igniter', 'spark plug', 'ignición'],
        '75': ['engine bleed', 'purga del motor'],
        '76': ['engine control', 'throttle', 'fadec', 'control del motor'],
        '77': ['engine indicator', 'egt', 'n1', 'n2', 'indicador del motor'],
        '78': ['exhaust', 'escape', 'tailpipe'],
        '79': ['engine oil', 'oil filter', 'aceite del motor'],
        '80': ['engine start', 'starter', 'arranque'],
    };

    // ================================================================
    // STATE
    // ================================================================
    let _requestId = 0;
    let _isServerless = false;

    const state = {
        selectedSource: 'federal_register',
        results: [],
        filteredResults: null,
        sortField: '_relevance',
        sortOrder: 'desc',
        currentPage: 1,
        pageSize: 25,
        searching: false,
        selectedIds: new Set(),
        activeManufacturerKey: null,
        activeManufacturerModels: null,
        lastResultCount: null,
        lastSearchMs: null,
        lastSearchTerm: '',
        lastErrors: [],
        lastParams: null,
        sourceCounts: {},
        streamAbort: null,
        viewMode: localStorage.getItem('viewMode') || 'table',
        quickFilter: '',
        ataFilter: '',
        analysisQueue: [],
        _lastRenderKey: null,
    };

    // ================================================================
    // DOM
    // ================================================================
    const $ = id => document.getElementById(id);
    const dom = {
        searchForm: $('search-form'),
        btnSearch: $('btn-search'),
        btnSearchText: document.querySelector('.btn-search-text'),
        btnSearchIcon: document.querySelector('.btn-search-icon'),
        btnSearchSpinner: document.querySelector('.btn-search-spinner'),
        btnClear: $('btn-clear'),
        resultsSection: $('results-section'),
        resultsBody: $('results-body'),
        resultsCount: $('results-count'),
        resultsTime: $('results-time'),
        pagination: $('pagination'),
        statusIndicator: $('status-indicator'),
        statusText: $('status-text'),
        btnExportCsv: $('btn-export-csv'),
        btnExportExcel: $('btn-export-excel'),
        toastContainer: $('toast-container'),
        themeToggle: $('theme-toggle'),
        selectAll: $('select-all'),
        bulkBar: $('bulk-bar'),
        bulkBarText: $('bulk-bar-text'),
        bulkFilterBadge: $('bulk-filter-badge'),
        bulkExportCsv: $('bulk-export-csv'),
        bulkExportExcel: $('bulk-export-excel'),
        bulkExportZip: $('bulk-export-zip'),
        bulkDeselect: $('bulk-deselect'),
        inputKeyword: $('input-keyword'),
        inputMake: $('input-make'),
        inputModel: $('input-model'),
        inputDateFrom: $('input-date-from'),
        inputDateTo: $('input-date-to'),
        emptyState: $('empty-state'),
        emptyStateTitle: $('empty-state-title'),
        emptyStateErrors: $('empty-state-errors'),
        restoreBanner: $('restore-banner'),
        restoreBannerText: $('restore-banner-text'),
        restoreSearchAgain: $('restore-search-again'),
        restoreDismiss: $('restore-dismiss'),
        sidebar: $('sidebar'),
        sidebarOverlay: $('sidebar-overlay'),
        mobileMenuBtn: $('mobile-menu-btn'),
        tableContainer: $('table-container'),
        cardsContainer: $('cards-container'),
        viewTableBtn: $('view-table-btn'),
        viewCardsBtn: $('view-cards-btn'),
        quickFilter: $('quick-filter'),
        btnShareUrl: $('btn-share-url'),
        recentSearches: $('recent-searches'),
        initialState: $('initial-state'),
        shortcutsModal: $('shortcuts-modal'),
        shortcutsModalClose: $('shortcuts-modal-close'),
        btnShortcutsHelp: $('btn-shortcuts-help'),
        confirmModal: $('confirm-modal'),
        confirmModalText: $('confirm-modal-text'),
        confirmModalOk: $('confirm-modal-ok'),
        confirmModalCancel: $('confirm-modal-cancel'),
        keywordClear: $('keyword-clear'),
        makeChipContainer: $('make-chip-container'),
        searchSummary: $('search-summary'),
        maxResultsSelect: $('max-results-select'),
        ataSelect: $('input-ata-chapter'),
        analysisBadgeBtn: $('analysis-badge-btn'),
        analysisCountBadge: $('analysis-count-badge'),
        analysisDrawer: $('analysis-drawer'),
        analysisOverlay: $('analysis-overlay'),
        analysisDrawerClose: $('analysis-drawer-close'),
        analysisDrawerBody: $('analysis-drawer-body'),
        btnAnalyzeAeris: $('btn-analyze-aeris'),
        aerisModal: $('aeris-modal'),
        aerisModalClose: $('aeris-modal-close'),
        aerisModalOk: $('aeris-modal-ok'),
    };

    // Max results preference
    const storedMax = localStorage.getItem('maxResults');
    if (storedMax && dom.maxResultsSelect) {
        dom.maxResultsSelect.value = storedMax;
    }
    if (dom.maxResultsSelect) {
        dom.maxResultsSelect.addEventListener('change', () => {
            localStorage.setItem('maxResults', dom.maxResultsSelect.value);
        });
    }
    function getMaxResults() {
        return dom.maxResultsSelect ? parseInt(dom.maxResultsSelect.value) || 2000 : 2000;
    }

    // ================================================================
    // THEME
    // ================================================================
    if ((localStorage.getItem('theme') || 'dark') === 'light') document.documentElement.classList.add('light-mode');
    if (dom.themeToggle) dom.themeToggle.addEventListener('click', () => {
        const isLight = document.documentElement.classList.toggle('light-mode');
        localStorage.setItem('theme', isLight ? 'light' : 'dark');
    });

    // ================================================================
    // MOBILE SIDEBAR
    // ================================================================
    function openSidebar() { dom.sidebar.classList.add('open'); dom.sidebarOverlay.classList.add('active'); }
    function closeSidebar() { dom.sidebar.classList.remove('open'); dom.sidebarOverlay.classList.remove('active'); }
    if (dom.mobileMenuBtn) dom.mobileMenuBtn.addEventListener('click', openSidebar);
    if (dom.sidebarOverlay) dom.sidebarOverlay.addEventListener('click', closeSidebar);

    // Keyword clear button
    if (dom.keywordClear && dom.inputKeyword) {
        dom.keywordClear.addEventListener('click', () => {
            dom.inputKeyword.value = '';
            dom.inputKeyword.focus();
        });
    }

    // ================================================================
    // SOURCE SELECTION
    // ================================================================
    document.querySelectorAll('.source-card').forEach(card => {
        card.addEventListener('click', () => {
            document.querySelectorAll('.source-card').forEach(c => c.classList.remove('active'));
            card.classList.add('active');
            state.selectedSource = card.dataset.source;
        });
    });

    // ================================================================
    // VIEW MODE TOGGLE
    // ================================================================
    function setViewMode(mode) {
        state.viewMode = mode;
        localStorage.setItem('viewMode', mode);
        if (dom.viewTableBtn) dom.viewTableBtn.classList.toggle('active', mode === 'table');
        if (dom.viewCardsBtn) dom.viewCardsBtn.classList.toggle('active', mode === 'cards');
        if (dom.tableContainer) dom.tableContainer.style.display = mode === 'table' ? '' : 'none';
        if (dom.cardsContainer) dom.cardsContainer.style.display = mode === 'cards' ? '' : 'none';
        state._lastRenderKey = null;
        if (state.results.length > 0) renderResults();
    }
    if (dom.viewTableBtn) dom.viewTableBtn.addEventListener('click', () => setViewMode('table'));
    if (dom.viewCardsBtn) dom.viewCardsBtn.addEventListener('click', () => setViewMode('cards'));
    setViewMode(state.viewMode);

    // ================================================================
    // DATE INPUTS — dd/mm/aaaa text formatting
    // ================================================================
    function setupDateInput(input) {
        if (!input) return;
        input.addEventListener('input', function (e) {
            let v = this.value.replace(/[^\d]/g, '');
            if (v.length > 8) v = v.slice(0, 8);
            if (v.length >= 5) v = v.slice(0,2) + '/' + v.slice(2,4) + '/' + v.slice(4);
            else if (v.length >= 3) v = v.slice(0,2) + '/' + v.slice(2);
            this.value = v;
            // Validate
            if (v.length === 10) {
                const valid = isValidDate(v);
                this.classList.toggle('input-error', !valid);
            } else {
                this.classList.remove('input-error');
            }
        });
    }
    function isValidDate(str) {
        const m = str.match(/^(\d{2})\/(\d{2})\/(\d{4})$/);
        if (!m) return false;
        const d = parseInt(m[1]), mo = parseInt(m[2]), y = parseInt(m[3]);
        if (mo < 1 || mo > 12 || d < 1 || d > 31 || y < 1900 || y > 2100) return false;
        const dt = new Date(y, mo - 1, d);
        return dt.getFullYear() === y && dt.getMonth() === mo - 1 && dt.getDate() === d;
    }
    function parseDateInput(str) {
        if (!str) return '';
        const m = str.match(/^(\d{2})\/(\d{2})\/(\d{4})$/);
        if (m) return `${m[3]}-${m[2]}-${m[1]}`;
        return str;
    }
    function formatDateForInput(isoStr) {
        if (!isoStr) return '';
        const m = isoStr.match(/^(\d{4})-(\d{2})-(\d{2})/);
        if (m) return `${m[3]}/${m[2]}/${m[1]}`;
        return isoStr;
    }
    setupDateInput(dom.inputDateFrom);
    setupDateInput(dom.inputDateTo);

    // ================================================================
    // QUICK FILTER
    // ================================================================
    if (dom.quickFilter) {
        dom.quickFilter.addEventListener('input', () => {
            state.quickFilter = dom.quickFilter.value.trim().toLowerCase();
            state.currentPage = 1;
            applyQuickFilter();
            state._lastRenderKey = null;
            renderResults();
        });
    }

    // Populate ATA select
    if (dom.ataSelect) {
        ATA_CHAPTERS.forEach(ch => {
            const opt = document.createElement('option');
            opt.value = ch.code;
            opt.textContent = `ATA ${ch.code} — ${ch.name}`;
            dom.ataSelect.appendChild(opt);
        });
        dom.ataSelect.addEventListener('change', () => {
            state.ataFilter = dom.ataSelect.value;
            state.currentPage = 1;
            applyClientFilters();
            state._lastRenderKey = null;
            renderResults();
            updateAtaIndicator();
        });
    }

    function updateAtaIndicator() {
        const label = document.querySelector('.form-group-label');
        if (!label) return;
        const labels = document.querySelectorAll('.form-group-label');
        for (const l of labels) {
            if (l.textContent.trim() === 'Filtros') {
                l.classList.toggle('ata-active', !!state.ataFilter);
                if (!l.querySelector('.ata-active-dot')) {
                    const dot = document.createElement('span');
                    dot.className = 'ata-active-dot';
                    l.appendChild(dot);
                }
                break;
            }
        }
    }

    function matchesAtaFilter(r) {
        if (!state.ataFilter) return true;
        const code = state.ataFilter;
        const keywords = ATA_KEYWORDS[code] || [];
        const text = `${r.AD_Number || ''} ${r.Subject || ''}`.toLowerCase();
        if (text.includes(`ata ${code}`) || text.includes(`ata-${code}`)) return true;
        for (const kw of keywords) {
            if (text.includes(kw.toLowerCase())) return true;
        }
        return false;
    }

    function applyClientFilters() {
        const hasQuick = !!state.quickFilter;
        const hasAta = !!state.ataFilter;
        if (!hasQuick && !hasAta) { state.filteredResults = null; return; }
        state.filteredResults = state.results.filter(r => {
            if (hasQuick) {
                const q = state.quickFilter;
                const match = (r.AD_Number || '').toLowerCase().includes(q) ||
                    (r.Make || '').toLowerCase().includes(q) ||
                    (r.Model || '').toLowerCase().includes(q) ||
                    (r.Subject || '').toLowerCase().includes(q);
                if (!match) return false;
            }
            if (hasAta && !matchesAtaFilter(r)) return false;
            return true;
        });
    }

    function applyQuickFilter() {
        applyClientFilters();
    }

    function getActiveResults() {
        return state.filteredResults !== null ? state.filteredResults : state.results;
    }

    // ================================================================
    // SEARCH — SSE STREAMING
    // ================================================================
    dom.searchForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        if (state.searching) {
            if (state.streamAbort) state.streamAbort.abort();
            state.searching = false;
        }

        // Validate dates
        const dfVal = dom.inputDateFrom ? dom.inputDateFrom.value : '';
        const dtVal = dom.inputDateTo ? dom.inputDateTo.value : '';
        if (dfVal && dfVal.length === 10 && !isValidDate(dfVal)) { showToast('Fecha "Desde" inválida.', 'error'); return; }
        if (dtVal && dtVal.length === 10 && !isValidDate(dtVal)) { showToast('Fecha "Hasta" inválida.', 'error'); return; }

        const params = {
            source: state.selectedSource,
            keyword: (dom.inputKeyword ? dom.inputKeyword.value : '').trim(),
            make: (dom.inputMake ? dom.inputMake.value : '').trim(),
            model: (dom.inputModel ? dom.inputModel.value : '').trim(),
            product_type: ($('input-product-type') ? $('input-product-type').value : ''),
            date_from: parseDateInput(dfVal),
            date_to: parseDateInput(dtVal),
            max_results: String(getMaxResults()),
        };

        const hasInput = params.keyword || params.make || params.model;
        if (!hasInput && params.source !== 'all') {
            showToast('Ingresa al menos un término de búsqueda.', 'error');
            return;
        }

        state.lastSearchTerm = params.keyword || params.make || params.model || '';
        state.lastParams = params;
        dismissRestoreBanner();
        closeSidebar();

        const qs = new URLSearchParams(params).toString();
        history.pushState(params, '', '/app?' + qs);

        if (_isServerless) await postSearch(params);
        else await streamSearch(params);
    });

    async function streamSearch(params) {
        const thisRequestId = ++_requestId;

        state.searching = true;
        state.currentPage = 1;
        state.sortField = '_relevance';
        state.sortOrder = 'desc';
        state.lastErrors = [];
        state.results = [];
        state.filteredResults = null;
        state.sourceCounts = {};
        state.quickFilter = '';
        state.ataFilter = '';
        state._lastRenderKey = null;
        if (dom.quickFilter) dom.quickFilter.value = '';
        if (dom.ataSelect) dom.ataSelect.value = '';
        clearSelection();

        showButtonLoading(true);
        setStatus('searching', 'Buscando...');
        updateTitle('searching');

        if (dom.emptyState) dom.emptyState.style.display = 'none';
        if (dom.initialState) dom.initialState.style.display = 'none';
        dom.resultsSection.style.display = 'block';
        showSkeleton();
        resetSourceCardStates(params.source);

        const t0 = performance.now();
        const qs = new URLSearchParams(params).toString();
        const abortCtrl = new AbortController();
        state.streamAbort = abortCtrl;

        const safetyTimeout = setTimeout(() => {
            if (state.searching && thisRequestId === _requestId) {
                abortCtrl.abort();
                state.lastSearchMs = Math.round(performance.now() - t0);
                state.lastResultCount = state.results.length;
                state.searching = false;
                state.streamAbort = null;
                showButtonLoading(false);
                if (state.results.length > 0) {
                    state._lastRenderKey = null;
                    renderResults();
                }
                showToast('Búsqueda completada — puede haber resultados parciales.', 'info');
                setStatus('ready', 'Listo');
                updateTitle('results');
            }
        }, 60000);

        try {
            const response = await fetch(`/api/search/stream?${qs}`, { signal: abortCtrl.signal });
            if (response.status === 429) {
                const errData = await response.json().catch(() => null);
                showToast(errData?.error || 'Límite de búsquedas alcanzado — actualiza tu plan para continuar', 'error');
                setStatus('ready', 'Listo'); updateTitle('default'); return;
            }
            if (!response.ok) throw new Error(`Error del servidor (${response.status})`);
            if (thisRequestId !== _requestId) return;

            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                if (thisRequestId !== _requestId) { reader.cancel(); return; }

                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n');
                buffer = lines.pop();

                for (const line of lines) {
                    if (!line.startsWith('data: ')) continue;
                    const jsonStr = line.slice(6).trim();
                    if (!jsonStr) continue;
                    try {
                        const event = JSON.parse(jsonStr);
                        if (event.event === 'done') continue;
                        if (thisRequestId !== _requestId) return;

                        const srcResults = event.results || [];
                        const srcError = event.error;
                        const srcCount = event.count || 0;

                        state.results.push(...srcResults);
                        state.sourceCounts[event.source] = srcCount;
                        updateSourceCard(event.source, srcCount, srcError);

                        if (srcError) { state.lastErrors.push(srcError); showToast(srcError, 'error'); }

                        state.lastResultCount = state.results.length;
                        state.lastSearchMs = Math.round(performance.now() - t0);
                        state._lastRenderKey = null;
                        renderResults();
                        updateTitle('results');
                    } catch (_) {}
                }
            }

            if (thisRequestId !== _requestId) return;

            state.lastSearchMs = Math.round(performance.now() - t0);
            state.lastResultCount = state.results.length;

            if (state.results.length > 0) {
                showToast(`${state.results.length.toLocaleString('es-ES')} resultados encontrados.`, 'success');
                state._lastRenderKey = null;
                renderResults();
                if (dom.emptyState) dom.emptyState.style.display = 'none';
            } else {
                dom.resultsSection.style.display = 'none';
                showEmptyState();
            }

            updateTitle('results');
            setStatus('ready', 'Listo');
            saveSession(params, state.results);
            saveRecentSearch(params, state.results.length);
            updateSearchSummary();

        } catch (error) {
            if (error.name === 'AbortError') return;
            if (thisRequestId !== _requestId) return;
            showToast(`Error: ${error.message}`, 'error');
            setStatus('error', 'Error');
            updateTitle('default');
        } finally {
            clearTimeout(safetyTimeout);
            if (thisRequestId === _requestId) {
                state.searching = false;
                state.streamAbort = null;
                showButtonLoading(false);
            }
        }
    }

    async function postSearch(params) {
        const thisRequestId = ++_requestId;
        state.searching = true;
        state.currentPage = 1;
        state.sortField = '_relevance';
        state.sortOrder = 'desc';
        state.lastErrors = [];
        state.results = [];
        state.filteredResults = null;
        state.sourceCounts = {};
        state.quickFilter = '';
        state.ataFilter = '';
        state._lastRenderKey = null;
        if (dom.quickFilter) dom.quickFilter.value = '';
        if (dom.ataSelect) dom.ataSelect.value = '';
        clearSelection();
        showButtonLoading(true);
        setStatus('searching', 'Buscando...');
        updateTitle('searching');
        if (dom.emptyState) dom.emptyState.style.display = 'none';
        if (dom.initialState) dom.initialState.style.display = 'none';
        dom.resultsSection.style.display = 'block';
        showSkeleton();

        const t0 = performance.now();
        const abortCtrl = new AbortController();
        state.streamAbort = abortCtrl;

        try {
            const response = await fetch('/api/search', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(params),
                signal: abortCtrl.signal,
            });
            if (response.status === 429) {
                const errData = await response.json().catch(() => null);
                showToast(errData?.error || 'Límite de búsquedas alcanzado — actualiza tu plan para continuar', 'error');
                setStatus('ready', 'Listo'); updateTitle('default'); return;
            }
            if (!response.ok) throw new Error(`Error del servidor (${response.status})`);
            if (thisRequestId !== _requestId) return;
            const data = await response.json();

            state.results = data.results || [];
            state.lastErrors = data.errors || [];
            state.lastSearchMs = Math.round(performance.now() - t0);
            state.lastResultCount = state.results.length;

            if (state.lastErrors.length > 0) {
                state.lastErrors.forEach(err => showToast(err, 'error'));
            }
            if (state.results.length > 0) {
                showToast(`${state.results.length.toLocaleString('es-ES')} resultados encontrados.`, 'success');
                state._lastRenderKey = null;
                renderResults();
                if (dom.emptyState) dom.emptyState.style.display = 'none';
            } else {
                dom.resultsSection.style.display = 'none';
                showEmptyState();
            }
            updateTitle('results');
            setStatus('ready', 'Listo');
            saveSession(params, state.results);
            saveRecentSearch(params, state.results.length);
            updateSearchSummary();
        } catch (error) {
            if (error.name === 'AbortError') return;
            if (thisRequestId !== _requestId) return;
            showToast(`Error: ${error.message}`, 'error');
            setStatus('error', 'Error');
            updateTitle('default');
        } finally {
            if (thisRequestId === _requestId) {
                state.searching = false;
                state.streamAbort = null;
                showButtonLoading(false);
            }
        }
    }

    // ================================================================
    // SOURCE CARD STATES
    // ================================================================
    function resetSourceCardStates(source) {
        const keys = source === 'all' ? ['federal_register','easa','anac_brazil','transport_canada','dgac_chile'] : [source];
        keys.forEach(key => {
            const el = $(`count-${key}`);
            if (el) { el.innerHTML = '<span class="source-card-spinner"></span>'; el.classList.remove('source-count--error'); el.classList.remove('source-count--loaded'); }
        });
    }
    function updateSourceCard(srcKey, count, error) {
        const el = $(`count-${srcKey}`);
        if (!el) return;
        if (error) { el.textContent = 'Error'; el.classList.add('source-count--error'); el.classList.remove('source-count--loaded'); }
        else { el.textContent = `${count.toLocaleString('es-ES')} ADs`; el.classList.remove('source-count--error'); if (count > 0) el.classList.add('source-count--loaded'); }
    }

    // ================================================================
    // RELEVANCE SCORING
    // ================================================================
    function calcRelevance(r) {
        const term = state.lastSearchTerm.toLowerCase();
        if (!term) return 1;
        const adNum = (r.AD_Number || '').toLowerCase();
        const subjectWords = (r.Subject || '').toLowerCase().split(/\s+/);
        const subjectFull = (r.Subject || '').toLowerCase();
        const first3 = subjectWords.slice(0, 3).join(' ');
        if (adNum.includes(term) || first3.includes(term)) return 3;
        if (subjectFull.includes(term)) return 2;
        return 1;
    }

    function relevanceBarsHtml(score) {
        const bars = [1,2,3].map(i => `<div class="rel-bar${i <= score ? ' filled' : ''}"></div>`).join('');
        const labels = {1:'Baja',2:'Media',3:'Alta'};
        return `<span class="relevance-bars" title="Relevancia: ${labels[score] || 'Baja'}">${bars}</span>`;
    }

    // ================================================================
    // SKELETON
    // ================================================================
    function showSkeleton() {
        const cols = 10;
        let html = '';
        for (let i = 0; i < 6; i++) {
            html += '<tr class="skeleton-row">';
            for (let j = 0; j < cols; j++) {
                const w = j === 0 ? 15 : (j === 7 ? 180 : 50 + Math.random() * 50);
                html += `<td><div class="skeleton-cell" style="width:${Math.round(w)}px"></div></td>`;
            }
            html += '</tr>';
        }
        dom.resultsBody.innerHTML = html;
    }

    // ================================================================
    // EMPTY STATE
    // ================================================================
    function showEmptyState() {
        if (!dom.emptyState) return;
        dom.resultsSection.style.display = 'none';
        dom.emptyStateTitle.textContent = state.lastSearchTerm ? `Sin resultados para "${state.lastSearchTerm}"` : 'Sin resultados';
        if (dom.emptyStateErrors && state.lastErrors.length > 0) {
            dom.emptyStateErrors.innerHTML = state.lastErrors.map(err =>
                `<div class="empty-error-item"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg><span>${escapeHtml(err)}</span></div>`
            ).join('');
        } else if (dom.emptyStateErrors) { dom.emptyStateErrors.innerHTML = ''; }
        dom.emptyState.style.display = 'block';
    }

    // ================================================================
    // BUTTON LOADING
    // ================================================================
    function showButtonLoading(loading) {
        if (!dom.btnSearch) return;
        dom.btnSearch.disabled = loading;
        if (dom.btnSearchText) dom.btnSearchText.textContent = loading ? 'Buscando...' : 'Buscar';
        if (dom.btnSearchIcon) dom.btnSearchIcon.style.display = loading ? 'none' : '';
        if (dom.btnSearchSpinner) dom.btnSearchSpinner.style.display = loading ? 'inline-block' : 'none';
    }

    // ================================================================
    // CLEAR
    // ================================================================
    dom.btnClear.addEventListener('click', () => {
        if (state.streamAbort) { state.streamAbort.abort(); state.streamAbort = null; }
        dom.searchForm.reset();
        if (dom.inputDateFrom) dom.inputDateFrom.value = '';
        if (dom.inputDateTo) dom.inputDateTo.value = '';
        state.results = []; state.filteredResults = null; state.currentPage = 1;
        state.activeManufacturerKey = null; state.activeManufacturerModels = null;
        state.lastErrors = []; state.sourceCounts = {}; state.lastParams = null;
        state.quickFilter = ''; state.ataFilter = ''; state._lastRenderKey = null; state.searching = false;
        if (dom.quickFilter) dom.quickFilter.value = '';
        if (dom.ataSelect) dom.ataSelect.value = '';
        clearSelection();
        showButtonLoading(false);
        dom.resultsSection.style.display = 'none';
        if (dom.emptyState) dom.emptyState.style.display = 'none';
        if (dom.initialState) dom.initialState.style.display = '';
        if (dom.inputModel) dom.inputModel.placeholder = 'Ej: 737, A320...';
        updateTitle('default');
        document.querySelectorAll('.source-count').forEach(el => { el.innerHTML = '&mdash;'; el.classList.remove('source-count--error'); el.classList.remove('source-count--loaded'); });
        dismissRestoreBanner();
        updateMakeChip();
        if (dom.searchSummary) { dom.searchSummary.classList.remove('visible'); dom.searchSummary.textContent = ''; }
        history.replaceState(null, '', '/app');
    });

    // ================================================================
    // SELECTION
    // ================================================================
    function clearSelection() { state.selectedIds.clear(); if (dom.selectAll) dom.selectAll.checked = false; updateBulkBar(); }
    function updateBulkBar() {
        const count = state.selectedIds.size;
        if (count > 0) {
            dom.bulkBar.style.display = 'flex';
            dom.bulkBarText.textContent = `${count} seleccionado${count > 1 ? 's' : ''}`;
            if (dom.bulkFilterBadge) dom.bulkFilterBadge.style.display = state.quickFilter ? '' : 'none';
        } else { dom.bulkBar.style.display = 'none'; }
    }

    function getPageAdNumbers() {
        const active = getActiveResults();
        const sorted = getSortedResults(active);
        const start = (state.currentPage - 1) * state.pageSize;
        const end = start + state.pageSize;
        const ads = [];
        for (let i = start; i < end && i < sorted.length; i++) {
            if (sorted[i].AD_Number) ads.push(sorted[i].AD_Number);
        }
        return ads;
    }

    function updateSelectAllCheckbox() {
        const pageAds = getPageAdNumbers();
        const allChecked = pageAds.length > 0 && pageAds.every(ad => state.selectedIds.has(ad));
        if (dom.selectAll) dom.selectAll.checked = allChecked;
    }
    if (dom.selectAll) {
        dom.selectAll.addEventListener('change', () => {
            const pageAds = getPageAdNumbers();
            for (const ad of pageAds) {
                if (dom.selectAll.checked) state.selectedIds.add(ad); else state.selectedIds.delete(ad);
            }
            state._lastRenderKey = null;
            renderResults(); updateBulkBar();
        });
    }
    if (dom.bulkDeselect) dom.bulkDeselect.addEventListener('click', () => { clearSelection(); state._lastRenderKey = null; renderResults(); });
    if (dom.bulkExportCsv) dom.bulkExportCsv.addEventListener('click', () => exportSelected('csv'));
    if (dom.bulkExportExcel) dom.bulkExportExcel.addEventListener('click', () => exportSelected('excel'));
    if (dom.bulkExportZip) dom.bulkExportZip.addEventListener('click', () => handleZipExport());

    // ================================================================
    // ZIP EXPORT — with PDF download + confirmation
    // ================================================================
    function showConfirmModal(message) {
        return new Promise((resolve) => {
            if (!dom.confirmModal) { resolve(true); return; }
            dom.confirmModalText.textContent = message;
            dom.confirmModal.style.display = 'flex';
            const cleanup = () => { dom.confirmModal.style.display = 'none'; };
            dom.confirmModalOk.onclick = () => { cleanup(); resolve(true); };
            dom.confirmModalCancel.onclick = () => { cleanup(); resolve(false); };
            dom.confirmModal.onclick = (e) => { if (e.target === dom.confirmModal) { cleanup(); resolve(false); } };
        });
    }

    async function handleZipExport() {
        if (state.selectedIds.size === 0) { showToast('No hay registros seleccionados.', 'error'); return; }
        const selectedResults = state.results.filter(r => state.selectedIds.has(r.AD_Number));
        const pdfCount = selectedResults.filter(r => r.PDF_Link).length;

        if (pdfCount > 20) {
            const confirmed = await showConfirmModal(`Vas a descargar ${pdfCount} PDFs. Esto puede tardar un momento. ¿Continuar?`);
            if (!confirmed) return;
        }

        // Button loading state
        if (dom.bulkExportZip) {
            dom.bulkExportZip.disabled = true;
            dom.bulkExportZip.innerHTML = '<span class="btn-search-spinner" style="display:inline-block;width:12px;height:12px"></span> Descargando...';
        }

        showToast(`Preparando ZIP con ${pdfCount} PDFs...`, 'info');

        try {
            const payload = { results: selectedResults };
            if (state.lastParams) payload.filters = state.lastParams;
            const response = await fetch('/api/export/zip', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            if (!response.ok) {
                const errData = await response.json().catch(() => null);
                throw new Error(errData?.error || `Error ${response.status}`);
            }
            const blob = await response.blob();
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            const ts = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
            a.download = `AD_Export_${ts}_${selectedResults.length}_documentos.zip`;
            document.body.appendChild(a); a.click(); document.body.removeChild(a);
            URL.revokeObjectURL(url);
            showToast(`ZIP descargado (${selectedResults.length} ADs, ${pdfCount} PDFs).`, 'success');
        } catch (error) {
            showToast(`Error ZIP: ${error.message}`, 'error');
        } finally {
            if (dom.bulkExportZip) {
                dom.bulkExportZip.disabled = false;
                dom.bulkExportZip.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="12" y1="18" x2="12" y2="12"/><polyline points="9 15 12 12 15 15"/></svg> Descargar PDFs';
            }
        }
    }

    async function exportSelected(format) {
        if (state.selectedIds.size === 0) { showToast('No hay registros seleccionados.', 'error'); return; }
        const selectedResults = state.results.filter(r => state.selectedIds.has(r.AD_Number));
        try {
            showToast(`Exportando ${format.toUpperCase()}...`, 'info');
            const payload = { results: selectedResults };
            const response = await fetch(`/api/export/${format}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
            if (!response.ok) throw new Error('Error al exportar');
            const blob = await response.blob();
            downloadBlob(blob, format, selectedResults.length);
        } catch (error) { showToast(`Error: ${error.message}`, 'error'); }
    }

    // ================================================================
    // RENDER
    // ================================================================
    function renderResults() {
        const active = getActiveResults();
        const sorted = getSortedResults(active);
        const totalPages = Math.ceil(sorted.length / state.pageSize);
        const start = (state.currentPage - 1) * state.pageSize;
        const end = start + state.pageSize;
        const pageResults = sorted.slice(start, end);

        const renderKey = `${state.currentPage}|${state.sortField}|${state.sortOrder}|${state.quickFilter}|${state.viewMode}|${state.selectedIds.size}|${active.length}|${state.results.length}`;
        if (state._lastRenderKey === renderKey) return;
        state._lastRenderKey = renderKey;

        if (state.filteredResults !== null) {
            dom.resultsCount.innerHTML = `Mostrando <strong>${active.length.toLocaleString('es-ES')}</strong> de ${state.results.length.toLocaleString('es-ES')}`;
        } else {
            dom.resultsCount.innerHTML = `<strong>${active.length.toLocaleString('es-ES')}</strong> resultados`;
        }
        if (dom.resultsTime) dom.resultsTime.textContent = state.lastSearchMs !== null ? `\u00b7 ${(state.lastSearchMs / 1000).toFixed(1)}s` : '';
        const existingBadge = dom.resultsSection.querySelector('.badge-serverless');
        if (_isServerless && !existingBadge) {
            const badge = document.createElement('span');
            badge.className = 'badge-serverless';
            badge.textContent = 'Modo rápido';
            badge.title = 'Resultados limitados a 200 por fuente en este modo';
            dom.resultsCount.parentNode.appendChild(badge);
        }

        if (state.viewMode === 'table') renderTable(pageResults);
        else renderCards(pageResults);

        renderPagination(totalPages);
        updateSortHeaders();
        updateSelectAllCheckbox();
        updateBulkBar();
    }

    function renderTable(pageResults) {
        dom.resultsBody.innerHTML = pageResults.map(r => {
            const adNum = r.AD_Number || '';
            const checked = state.selectedIds.has(adNum) ? 'checked' : '';
            const rel = calcRelevance(r);
            return `<tr class="${checked ? 'row-selected' : ''}">
                <td class="td-checkbox"><input type="checkbox" class="row-checkbox" data-ad="${esc(adNum)}" ${checked}></td>
                <td class="col-rel" style="text-align:center">${relevanceBarsHtml(rel)}</td>
                <td>${getSourceBadge(r.Source)}</td>
                <td class="td-ad-number" title="${esc(adNum)}">${esc(adNum)}<button class="copy-ad-btn" data-ad="${esc(adNum)}" title="Copiar AD#">Copiar</button></td>
                <td class="td-make col-make" title="${esc(r.Make)}">${esc(r.Make)}</td>
                <td class="td-model col-model" title="${esc(r.Model)}">${esc(r.Model)}</td>
                <td class="col-tipo">${getTypeBadge(r.Product_Type)}</td>
                <td class="td-subject" title="${esc(r.Subject)}">${formatSubject(r.Subject, 75)}</td>
                <td class="td-date col-date">${formatDate(r.Effective_Date)}</td>
                <td class="td-actions">${pdfCell(r)}<button class="btn-add-analysis" data-ad="${esc(adNum)}" title="Agregar al análisis">＋ Análisis</button></td>
            </tr>`;
        }).join('');
    }

    function renderCards(pageResults) {
        dom.cardsContainer.innerHTML = pageResults.map(r => {
            const adNum = r.AD_Number || '';
            const checked = state.selectedIds.has(adNum) ? 'checked' : '';
            const rel = calcRelevance(r);
            return `<div class="result-card ${checked ? 'row-selected' : ''}">
                <div class="result-card-top">
                    <div class="result-card-badge">${getSourceBadge(r.Source)}</div>
                    <div class="result-card-actions">${relevanceBarsHtml(rel)}<input type="checkbox" class="row-checkbox" data-ad="${esc(adNum)}" ${checked}></div>
                </div>
                <div class="card-ad-number">${esc(adNum)}</div>
                <div class="card-make-model">${esc(r.Make)} ${esc(r.Model)} ${getTypeBadge(r.Product_Type)}</div>
                <div class="card-subject">${esc(r.Subject)}</div>
                <div class="result-card-bottom">
                    <span class="card-date">${formatDate(r.Effective_Date)}</span>
                    ${pdfCellCard(r)}
                </div>
            </div>`;
        }).join('');
    }

    // Delegated checkbox handler (table)
    dom.resultsBody.addEventListener('change', function (e) {
        if (e.target.matches('.row-checkbox')) {
            const ad = e.target.dataset.ad;
            if (e.target.checked) state.selectedIds.add(ad); else state.selectedIds.delete(ad);
            const row = e.target.closest('tr');
            if (row) row.classList.toggle('row-selected', e.target.checked);
            updateSelectAllCheckbox(); updateBulkBar();
        }
    });
    // Delegated checkbox handler (cards)
    if (dom.cardsContainer) dom.cardsContainer.addEventListener('change', function (e) {
        if (e.target.matches('.row-checkbox')) {
            const ad = e.target.dataset.ad;
            if (e.target.checked) state.selectedIds.add(ad); else state.selectedIds.delete(ad);
            const card = e.target.closest('.result-card');
            if (card) card.classList.toggle('row-selected', e.target.checked);
            updateSelectAllCheckbox(); updateBulkBar();
        }
    });
    // Delegated copy AD# handler
    dom.resultsBody.addEventListener('click', function (e) {
        const copyBtn = e.target.closest('.copy-ad-btn');
        if (copyBtn) {
            e.stopPropagation();
            const ad = copyBtn.dataset.ad;
            navigator.clipboard.writeText(ad).then(() => {
                showMicroToast(`${ad} copiado`);
            }).catch(() => showMicroToast('Error al copiar'));
            return;
        }
        const analysisBtn = e.target.closest('.btn-add-analysis');
        if (analysisBtn) {
            e.stopPropagation();
            addToAnalysis(analysisBtn.dataset.ad);
            return;
        }
    });

    function showMicroToast(msg) {
        const t = document.createElement('div');
        t.className = 'toast toast-success';
        t.innerHTML = `<svg class="toast-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 6L9 17l-5-5"/></svg><span class="toast-message">${esc(msg)}</span>`;
        dom.toastContainer.appendChild(t);
        setTimeout(() => { t.classList.add('removing'); setTimeout(() => t.remove(), 250); }, 1500);
    }

    function pdfCell(r) {
        return r.PDF_Link
            ? `<a href="${esc(r.PDF_Link)}" target="_blank" rel="noopener" class="pdf-link">Ver</a>`
            : '<span style="color:var(--t20)">—</span>';
    }

    function pdfCellCard(r) {
        return r.PDF_Link
            ? `<a href="${esc(r.PDF_Link)}" target="_blank" rel="noopener" class="pdf-link">Ver</a>`
            : '<span class="card-no-link">Sin enlace</span>';
    }

    function formatSubject(raw, maxLen) {
        if (!raw) return '';
        const truncated = raw.length > maxLen ? raw.slice(0, maxLen) + '...' : raw;
        const words = truncated.split(/\s+/);
        if (words.length <= 2) return `<span class="subject-lead">${esc(truncated)}</span>`;
        return `<span class="subject-lead">${esc(words.slice(0,2).join(' '))}</span> <span class="subject-rest">${esc(words.slice(2).join(' '))}</span>`;
    }

    function getTypeBadge(type) {
        if (!type) return '<span class="type-badge badge-type-unknown">\u2014</span>';
        const t = type.trim();
        if (/aircraft/i.test(t)) return '<span class="type-badge badge-type-aircraft">Aircraft</span>';
        if (/engine/i.test(t)) return '<span class="type-badge badge-type-engine">Engine</span>';
        if (/propeller/i.test(t)) return '<span class="type-badge badge-type-propeller">Propeller</span>';
        return `<span class="type-badge badge-type-unknown">${esc(t)}</span>`;
    }
    function getSourceBadge(source) {
        if (!source) return '';
        if (source.includes('Federal') || source.includes('FAA')) return '<span class="source-badge badge-fr">FAA</span>';
        if (source.includes('EASA')) return '<span class="source-badge badge-easa">EASA</span>';
        if (source.includes('Canada') || source.includes('TC')) return '<span class="source-badge badge-tc">TC</span>';
        if (source.includes('ANAC') || source.includes('Brasil')) return '<span class="source-badge badge-anac">ANAC</span>';
        if (source.includes('DGAC') || source.includes('Chile')) return '<span class="source-badge badge-dgac">DGAC</span>';
        return `<span class="source-badge">${esc(source)}</span>`;
    }

    // ================================================================
    // SORTING
    // ================================================================
    document.querySelectorAll('.results-table th.sortable').forEach(th => {
        th.addEventListener('click', () => {
            const field = th.dataset.sort;
            if (state.sortField === field) state.sortOrder = state.sortOrder === 'asc' ? 'desc' : 'asc';
            else { state.sortField = field; state.sortOrder = field === '_relevance' ? 'desc' : 'asc'; }
            state.currentPage = 1;
            state._lastRenderKey = null;
            renderResults();
        });
    });

    function getSortedResults(active) {
        const arr = [...(active || getActiveResults())];
        if (!state.sortField) return arr;
        if (state.sortField === '_relevance') {
            return arr.sort((a, b) => {
                const ra = calcRelevance(a), rb = calcRelevance(b);
                return state.sortOrder === 'desc' ? rb - ra : ra - rb;
            });
        }
        return arr.sort((a, b) => {
            const va = (a[state.sortField] || '').toString().toLowerCase();
            const vb = (b[state.sortField] || '').toString().toLowerCase();
            const cmp = va.localeCompare(vb);
            return state.sortOrder === 'asc' ? cmp : -cmp;
        });
    }
    function updateSortHeaders() {
        document.querySelectorAll('.results-table th.sortable').forEach(th => {
            th.classList.remove('asc', 'desc');
            if (th.dataset.sort === state.sortField) th.classList.add(state.sortOrder);
        });
    }

    // ================================================================
    // PAGINATION
    // ================================================================
    function renderPagination(totalPages) {
        if (totalPages <= 1) { dom.pagination.innerHTML = ''; return; }
        let html = `<button ${state.currentPage === 1 ? 'disabled' : ''} data-page="${state.currentPage - 1}">&lt;</button>`;
        const maxV = 7;
        let s = Math.max(1, state.currentPage - Math.floor(maxV / 2));
        let e = Math.min(totalPages, s + maxV - 1);
        if (e - s < maxV - 1) s = Math.max(1, e - maxV + 1);
        if (s > 1) { html += '<button data-page="1">1</button>'; if (s > 2) html += '<button disabled>...</button>'; }
        for (let i = s; i <= e; i++) html += `<button data-page="${i}" class="${i === state.currentPage ? 'active' : ''}">${i}</button>`;
        if (e < totalPages) { if (e < totalPages - 1) html += '<button disabled>...</button>'; html += `<button data-page="${totalPages}">${totalPages}</button>`; }
        html += `<button ${state.currentPage === totalPages ? 'disabled' : ''} data-page="${state.currentPage + 1}">&gt;</button>`;
        dom.pagination.innerHTML = html;
        dom.pagination.querySelectorAll('button[data-page]').forEach(btn => {
            btn.addEventListener('click', () => {
                const page = parseInt(btn.dataset.page);
                if (page >= 1 && page <= totalPages) { state.currentPage = page; state._lastRenderKey = null; renderResults(); dom.resultsSection.scrollIntoView({ behavior: 'smooth', block: 'start' }); }
            });
        });
    }

    // ================================================================
    // EXPORT (all visible / filtered)
    // ================================================================
    dom.btnExportCsv.addEventListener('click', () => exportAll('csv'));
    dom.btnExportExcel.addEventListener('click', () => exportAll('excel'));

    async function exportAll(format) {
        const data = getActiveResults();
        if (data.length === 0) { showToast('No hay resultados para exportar.', 'error'); return; }
        try {
            showToast(`Exportando ${format.toUpperCase()}...`, 'info');
            const response = await fetch(`/api/export/${format}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ results: data }) });
            if (!response.ok) throw new Error('Error al exportar');
            const blob = await response.blob();
            downloadBlob(blob, format, data.length);
        } catch (error) { showToast(`Error: ${error.message}`, 'error'); }
    }

    function downloadBlob(blob, format, count) {
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        const ext = format === 'excel' ? 'xlsx' : format;
        const ts = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
        a.download = `ad_results_${ts}.${ext}`;
        document.body.appendChild(a); a.click(); document.body.removeChild(a);
        URL.revokeObjectURL(url);
        showToast(`${ext.toUpperCase()} descargado (${count} registros).`, 'success');
    }

    // ================================================================
    // SHARE URL
    // ================================================================
    if (dom.btnShareUrl) dom.btnShareUrl.addEventListener('click', copySearchUrl);
    function copySearchUrl() {
        navigator.clipboard.writeText(window.location.href).then(() => {
            showToast('URL copiada al portapapeles.', 'success');
        }).catch(() => showToast('No se pudo copiar la URL.', 'error'));
    }

    // ================================================================
    // URL PARAMS
    // ================================================================
    function loadFromUrlParams() {
        const params = new URLSearchParams(window.location.search);
        if (!params.has('source') && !params.has('make') && !params.has('keyword')) return false;

        const p = {
            source: params.get('source') || 'federal_register',
            keyword: params.get('keyword') || '',
            make: params.get('make') || '',
            model: params.get('model') || '',
            product_type: params.get('product_type') || '',
            date_from: params.get('date_from') || '',
            date_to: params.get('date_to') || '',
            max_results: params.get('max_results') || String(getMaxResults()),
        };

        if (p.max_results && dom.maxResultsSelect) dom.maxResultsSelect.value = p.max_results;
        fillForm(p);
        state.lastParams = p;
        state.lastSearchTerm = p.keyword || p.make || p.model || '';
        if (_isServerless) postSearch(p); else streamSearch(p);
        return true;
    }

    window.addEventListener('popstate', (e) => {
        if (e.state && e.state.source) {
            fillForm(e.state);
            state.lastParams = e.state;
            state.lastSearchTerm = e.state.keyword || e.state.make || e.state.model || '';
            if (_isServerless) postSearch(e.state); else streamSearch(e.state);
        }
    });

    // ================================================================
    // SESSION STORAGE
    // ================================================================
    const SESSION_KEY = 'ad_search_session';
    const SESSION_TTL = 30 * 60 * 1000;
    function saveSession(params, results) { try { sessionStorage.setItem(SESSION_KEY, JSON.stringify({ params, results, timestamp: Date.now() })); } catch (_) {} }
    function loadSession() {
        try { const raw = sessionStorage.getItem(SESSION_KEY); if (!raw) return null; const d = JSON.parse(raw); if (Date.now() - d.timestamp > SESSION_TTL) { sessionStorage.removeItem(SESSION_KEY); return null; } return d; } catch (_) { return null; }
    }
    function restoreSession() {
        const session = loadSession();
        if (!session || !session.results || session.results.length === 0) return;
        state.results = session.results;
        state.lastParams = session.params;
        state.lastResultCount = session.results.length;
        state.lastSearchTerm = session.params.keyword || session.params.make || session.params.model || '';
        fillForm(session.params);
        state._lastRenderKey = null;
        renderResults();
        dom.resultsSection.style.display = 'block';
        if (dom.initialState) dom.initialState.style.display = 'none';
        updateTitle('results');
        setStatus('ready', 'Listo');
        const ago = Math.round((Date.now() - session.timestamp) / 60000);
        const agoLabel = ago < 1 ? 'hace menos de 1 min' : `hace ${ago} min`;
        if (dom.restoreBanner) { dom.restoreBannerText.textContent = `Resultados anteriores (${agoLabel})`; dom.restoreBanner.style.display = 'flex'; }
    }
    function dismissRestoreBanner() { if (dom.restoreBanner) dom.restoreBanner.style.display = 'none'; }
    if (dom.restoreDismiss) dom.restoreDismiss.addEventListener('click', dismissRestoreBanner);
    if (dom.restoreSearchAgain) dom.restoreSearchAgain.addEventListener('click', () => { dismissRestoreBanner(); dom.searchForm.dispatchEvent(new Event('submit')); });

    // ================================================================
    // RECENT SEARCHES (with result count preview — Bloque 3.3)
    // ================================================================
    const RECENT_KEY = 'recentSearches';
    const MAX_RECENT = 5;

    function loadRecent() { try { return JSON.parse(localStorage.getItem(RECENT_KEY)) || []; } catch(_) { return []; } }
    function saveRecent(items) { localStorage.setItem(RECENT_KEY, JSON.stringify(items)); }

    function saveRecentSearch(params, count) {
        const items = loadRecent();
        const parts = [];
        if (params.make) parts.push(params.make);
        if (params.model) parts.push(params.model);
        if (params.keyword && !parts.length) parts.push(params.keyword);
        const label = parts.join(' \u00b7 ') || params.source.replace('_',' ');
        const entry = { label, params, count, ts: Date.now() };
        const filtered = items.filter(i => i.label !== label);
        filtered.unshift(entry);
        saveRecent(filtered.slice(0, MAX_RECENT));
        renderRecentSearches();
    }

    function renderRecentSearches() {
        if (!dom.recentSearches) return;
        const items = loadRecent();
        if (items.length === 0) { dom.recentSearches.innerHTML = '<div class="recent-empty"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>Sin búsquedas recientes</div>'; return; }
        dom.recentSearches.innerHTML = items.map((item, i) => {
            const countLabel = item.count != null ? `${item.count} ADs` : '';
            return `<div class="recent-item" data-idx="${i}">
                <svg class="recent-item-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
                <span class="recent-item-text" title="${esc(item.label)}${countLabel ? ' — ' + countLabel : ''}">${esc(item.label)}${countLabel ? ' <span style="color:var(--t25)">— ' + countLabel + '</span>' : ''}</span>
                <button class="recent-item-remove" data-idx="${i}" title="Eliminar">&times;</button>
            </div>`;
        }).join('');

        dom.recentSearches.querySelectorAll('.recent-item').forEach(el => {
            el.addEventListener('click', (e) => {
                if (e.target.closest('.recent-item-remove')) return;
                const idx = parseInt(el.dataset.idx);
                const item = loadRecent()[idx];
                if (item && item.params) {
                    fillForm(item.params);
                    state.selectedSource = item.params.source;
                    document.querySelectorAll('.source-card').forEach(c => c.classList.toggle('active', c.dataset.source === item.params.source));
                    dom.searchForm.dispatchEvent(new Event('submit'));
                }
            });
        });
        dom.recentSearches.querySelectorAll('.recent-item-remove').forEach(btn => {
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const idx = parseInt(btn.dataset.idx);
                const items = loadRecent();
                items.splice(idx, 1);
                saveRecent(items);
                renderRecentSearches();
            });
        });
    }

    // ================================================================
    // FORM HELPERS
    // ================================================================
    function fillForm(p) {
        const setVal = (id, val) => { const el = $(id); if (el) el.value = val || ''; };
        setVal('input-keyword', p.keyword);
        setVal('input-make', p.make);
        setVal('input-model', p.model);
        setVal('input-product-type', p.product_type);
        // Convert ISO dates to dd/mm/aaaa for display
        if (dom.inputDateFrom) dom.inputDateFrom.value = formatDateForInput(p.date_from);
        if (dom.inputDateTo) dom.inputDateTo.value = formatDateForInput(p.date_to);
        if (p.source) {
            document.querySelectorAll('.source-card').forEach(c => c.classList.toggle('active', c.dataset.source === p.source));
            state.selectedSource = p.source;
        }
    }

    function updateSearchSummary() {
        if (!dom.searchSummary) return;
        if (!state.lastParams || !state.lastResultCount) {
            dom.searchSummary.classList.remove('visible');
            return;
        }
        const p = state.lastParams;
        const parts = [];
        const sourceNames = {federal_register:'Federal Register',easa:'EASA',transport_canada:'Transport Canada',anac_brazil:'ANAC Brasil',all:'Todas'};
        parts.push(sourceNames[p.source] || p.source);
        if (p.keyword) parts.push(p.keyword);
        if (p.make) parts.push(p.make);
        if (p.model) parts.push(p.model);
        dom.searchSummary.textContent = parts.join(' \u00b7 ');
        dom.searchSummary.classList.add('visible');
    }

    // ================================================================
    // AUTOCOMPLETE — Inverted index
    // ================================================================
    const _acIndex = { make: new Map(), model: new Map() };
    (function buildAutocompleteIndex() {
        function addToIndex(map, text, ref) {
            const lower = text.toLowerCase();
            for (let len = 2; len <= Math.min(lower.length, 8); len++) {
                for (let start = 0; start <= lower.length - len; start++) {
                    const prefix = lower.substring(start, start + len);
                    if (!map.has(prefix)) map.set(prefix, []);
                    const arr = map.get(prefix);
                    if (arr.indexOf(ref) === -1) arr.push(ref);
                }
            }
        }
        if (typeof AIRCRAFT_DATA === 'undefined') return;
        for (const [key, entry] of Object.entries(AIRCRAFT_DATA)) {
            addToIndex(_acIndex.make, key, key);
            for (const alias of entry.aliases) addToIndex(_acIndex.make, alias, key);
            for (const model of entry.models) addToIndex(_acIndex.model, model, { make: key, model });
        }
    })();

    function createAutocomplete(input, getSuggestions, onSelect, minChars = 2) {
        if (!input) return;
        const dropdown = document.createElement('div');
        dropdown.className = 'autocomplete-dropdown';
        dropdown.style.display = 'none';
        input.parentNode.style.position = 'relative';
        input.parentNode.appendChild(dropdown);
        let activeIndex = -1, currentItems = [];

        function renderDropdown(items) {
            currentItems = items;
            if (!items.length) { dropdown.style.display = 'none'; return; }
            dropdown.innerHTML = items.map((item, i) => `<div class="autocomplete-item" data-index="${i}" data-value="${esc(item.value)}">${item.highlight || esc(item.label)}</div>`).join('');
            dropdown.style.display = 'block';
            activeIndex = -1;
            dropdown.querySelectorAll('.autocomplete-item').forEach(el => {
                el.addEventListener('mousedown', (e) => { e.preventDefault(); onSelect(currentItems[parseInt(el.dataset.index)]); dropdown.style.display = 'none'; });
            });
        }
        function highlightText(text, query) {
            const idx = text.toLowerCase().indexOf(query.toLowerCase());
            if (idx === -1) return esc(text);
            return `${esc(text.slice(0,idx))}<strong>${esc(text.slice(idx,idx+query.length))}</strong>${esc(text.slice(idx+query.length))}`;
        }
        function update() {
            const q = input.value.trim();
            if (q.length < minChars) { dropdown.style.display = 'none'; return; }
            const raw = getSuggestions(q);
            renderDropdown(raw.map(i => ({ ...i, highlight: highlightText(i.label, q) })));
        }
        input.addEventListener('keydown', (e) => {
            const items = dropdown.querySelectorAll('.autocomplete-item');
            if (e.key === 'Escape') { dropdown.style.display = 'none'; activeIndex = -1; }
            else if (e.key === 'ArrowDown' && dropdown.style.display !== 'none') { e.preventDefault(); activeIndex = Math.min(activeIndex+1, items.length-1); items.forEach((el,i)=>el.classList.toggle('autocomplete-item--active',i===activeIndex)); if(items[activeIndex]) items[activeIndex].scrollIntoView({block:'nearest'}); }
            else if (e.key === 'ArrowUp' && dropdown.style.display !== 'none') { e.preventDefault(); activeIndex = Math.max(activeIndex-1,-1); items.forEach((el,i)=>el.classList.toggle('autocomplete-item--active',i===activeIndex)); }
            else if ((e.key==='Enter'||e.key==='Tab') && activeIndex>=0 && dropdown.style.display!=='none') { e.preventDefault(); onSelect(currentItems[activeIndex]); dropdown.style.display='none'; activeIndex=-1; }
        });
        input.addEventListener('input', update);
        input.addEventListener('focus', update);
        input.addEventListener('blur', () => { setTimeout(()=>{dropdown.style.display='none';activeIndex=-1},150); });
        return { update, close: () => { dropdown.style.display = 'none'; } };
    }

    function findManufacturer(query) {
        if (typeof AIRCRAFT_DATA === 'undefined') return null;
        const q = query.toLowerCase().trim();
        for (const [k,e] of Object.entries(AIRCRAFT_DATA)) { if (k.toLowerCase()===q || e.aliases.some(a=>a.toLowerCase()===q)) return {key:k,entry:e}; }
        for (const [k,e] of Object.entries(AIRCRAFT_DATA)) { if (k.toLowerCase().includes(q)||q.includes(k.toLowerCase())) return {key:k,entry:e}; }
        return null;
    }
    function updateModelPlaceholder() {
        if (!dom.inputModel) return;
        if (state.activeManufacturerModels && state.activeManufacturerModels.length > 0) dom.inputModel.placeholder = `Ej: ${state.activeManufacturerModels.slice(0,3).join(', ')}...`;
        else dom.inputModel.placeholder = 'Ej: 737, A320...';
    }

    function updateMakeChip() {
        if (!dom.makeChipContainer) return;
        if (state.activeManufacturerKey) {
            dom.makeChipContainer.innerHTML = `<div class="manufacturer-chip"><span>${esc(state.activeManufacturerKey)}</span><button type="button" class="chip-remove" title="Quitar">&times;</button></div>`;
            dom.makeChipContainer.querySelector('.chip-remove').addEventListener('click', () => {
                state.activeManufacturerKey = null;
                state.activeManufacturerModels = null;
                dom.inputMake.value = '';
                updateModelPlaceholder();
                updateMakeChip();
            });
        } else {
            dom.makeChipContainer.innerHTML = '';
        }
    }

    const makeAC = createAutocomplete(dom.inputMake, (q) => {
        const ql = q.toLowerCase();
        const hits = _acIndex.make.get(ql.substring(0, Math.min(ql.length, 8)));
        if (hits) {
            const unique = [...new Set(hits)].filter(k => k.toLowerCase().includes(ql));
            return unique.slice(0, 6).map(k => ({label:k, value:k}));
        }
        if (typeof AIRCRAFT_DATA === 'undefined') return [];
        const m = [];
        for (const [k,e] of Object.entries(AIRCRAFT_DATA)) { if (k.toLowerCase().includes(ql)||e.aliases.some(a=>a.includes(ql))) m.push({label:k,value:k}); }
        return m.slice(0,6);
    }, (item) => {
        dom.inputMake.value = item.value;
        const found = findManufacturer(item.value);
        state.activeManufacturerKey = found ? found.key : null;
        state.activeManufacturerModels = found ? found.entry.models : null;
        if (dom.inputModel) dom.inputModel.value = '';
        updateModelPlaceholder();
        updateMakeChip();
    });

    if (dom.inputMake) dom.inputMake.addEventListener('input', () => {
        const val = dom.inputMake.value.trim();
        if (!val) { state.activeManufacturerKey=null;state.activeManufacturerModels=null;updateModelPlaceholder();updateMakeChip();return; }
        const found=findManufacturer(val);state.activeManufacturerKey=found?found.key:null;state.activeManufacturerModels=found?found.entry.models:null;updateModelPlaceholder();updateMakeChip();
    });

    const modelAC = createAutocomplete(dom.inputModel, (q) => {
        const ql = q.toLowerCase();
        const pool = state.activeManufacturerModels;
        if (pool) {
            return pool.filter(m => m.toLowerCase().includes(ql)).slice(0,6).map(m => ({label:m,value:m}));
        }
        const hits = _acIndex.model.get(ql.substring(0, Math.min(ql.length, 8)));
        if (hits) {
            const unique = []; const seen = new Set();
            for (const h of hits) {
                if (h.model.toLowerCase().includes(ql) && !seen.has(h.model)) {
                    seen.add(h.model); unique.push(h.model);
                    if (unique.length >= 6) break;
                }
            }
            return unique.map(m => ({label:m,value:m}));
        }
        return [];
    }, (item) => { dom.inputModel.value = item.value; });

    // ================================================================
    // KEYBOARD SHORTCUTS
    // ================================================================
    function isInputFocused() {
        const el = document.activeElement;
        return el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT' || el.isContentEditable);
    }
    function closeAllDropdowns() {
        document.querySelectorAll('.autocomplete-dropdown').forEach(d => d.style.display = 'none');
    }
    function showShortcutsModal() { if (dom.shortcutsModal) dom.shortcutsModal.style.display = 'flex'; }
    function hideShortcutsModal() { if (dom.shortcutsModal) dom.shortcutsModal.style.display = 'none'; }

    if (dom.shortcutsModalClose) dom.shortcutsModalClose.addEventListener('click', hideShortcutsModal);
    if (dom.shortcutsModal) dom.shortcutsModal.addEventListener('click', (e) => { if (e.target === dom.shortcutsModal) hideShortcutsModal(); });
    if (dom.btnShortcutsHelp) dom.btnShortcutsHelp.addEventListener('click', showShortcutsModal);

    document.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && e.ctrlKey) { dom.searchForm.dispatchEvent(new Event('submit')); return; }
        if (e.key === 'C' && e.ctrlKey && e.shiftKey) { e.preventDefault(); copySearchUrl(); return; }
        if (e.key === 'a' && e.ctrlKey && !e.shiftKey && !isInputFocused()) {
            e.preventDefault();
            if (dom.selectAll) { dom.selectAll.checked = true; dom.selectAll.dispatchEvent(new Event('change')); }
            return;
        }
        if (isInputFocused()) return;
        if (e.key === '/') { e.preventDefault(); if (dom.inputKeyword) dom.inputKeyword.focus(); return; }
        if (e.key === '?') { e.preventDefault(); showShortcutsModal(); return; }
        if (e.key === 'Escape') {
            if (dom.shortcutsModal && dom.shortcutsModal.style.display !== 'none') { hideShortcutsModal(); return; }
            if (dom.sidebar && dom.sidebar.classList.contains('open')) { closeSidebar(); return; }
            const openDd = document.querySelector('.autocomplete-dropdown[style*="block"]');
            if (openDd) { closeAllDropdowns(); return; }
            clearSelection(); state._lastRenderKey = null; renderResults();
        }
    });

    // ================================================================
    // DYNAMIC TITLE
    // ================================================================
    const _defaultFavicon = '/static/img/favicon.svg';
    const _spinnerFavicon = "data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Cstyle%3Ecircle%7Bfill:none;stroke:%23fff;stroke-width:2.5;stroke-linecap:round;stroke-dasharray:50;stroke-dashoffset:35;animation:s .8s linear infinite%7D@keyframes s%7Bto%7Btransform:rotate(360deg)%7D%7D%3C/style%3E%3Ccircle cx='12' cy='12' r='9' transform-origin='12 12'/%3E%3C/svg%3E";

    function setFavicon(href) {
        let link = document.querySelector("link[rel='icon'][type='image/svg+xml']");
        if (link) link.href = href;
    }

    function updateTitle(mode) {
        if (mode === 'searching') { document.title = 'Buscando... \u2014 Zephr'; setFavicon(_spinnerFavicon); }
        else if (mode === 'results' && state.lastResultCount > 0) { document.title = `${state.lastResultCount.toLocaleString('es-ES')} ADs \u2014 Zephr`; setFavicon(_defaultFavicon); }
        else { document.title = DEFAULT_TITLE; setFavicon(_defaultFavicon); }
    }

    // ================================================================
    // STATUS / TOASTS
    // ================================================================
    function setStatus(type, text) {
        let label = text;
        if (type === 'ready' && state.lastResultCount !== null) label = `${state.lastResultCount.toLocaleString('es-ES')} ADs \u00b7 ${text}`;
        dom.statusText.textContent = label;
        dom.statusIndicator.className = 'status-dot';
        if (type === 'searching') dom.statusIndicator.classList.add('searching');
        if (type === 'error') dom.statusIndicator.classList.add('error');
    }
    function showToast(message, type = 'info') {
        const toast = document.createElement('div');
        toast.className = `toast toast-${type}`;
        const icons = {
            success: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 6L9 17l-5-5"/></svg>',
            error: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>',
            info: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/></svg>',
        };
        toast.innerHTML = `<div class="toast-icon">${icons[type]||icons.info}</div><span class="toast-message">${esc(message)}</span>`;
        dom.toastContainer.appendChild(toast);
        setTimeout(() => { toast.classList.add('removing'); setTimeout(() => toast.remove(), 250); }, 4000);
    }

    // ================================================================
    // UTILITIES
    // ================================================================
    function formatDate(str) {
        if (!str) return '<span style="color:var(--t20)">\u2014</span>';
        const M = ['Ene','Feb','Mar','Abr','May','Jun','Jul','Ago','Sep','Oct','Nov','Dic'];
        const m = str.match(/^(\d{4})-(\d{2})-(\d{2})/);
        if (m) { const d = new Date(+m[1],+m[2]-1,+m[3]); if (!isNaN(d)) return `${d.getDate()} ${M[d.getMonth()]} ${d.getFullYear()}`; }
        const m2 = str.match(/^(\d{1,2})\/(\d{2})\/(\d{4})/);
        if (m2) { const d = new Date(+m2[3],+m2[2]-1,+m2[1]); if (!isNaN(d)) return `${d.getDate()} ${M[d.getMonth()]} ${d.getFullYear()}`; }
        return esc(str);
    }
    function escapeHtml(s) { if (!s) return ''; const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
    const esc = escapeHtml;

    // ================================================================
    // ANALYSIS QUEUE
    // ================================================================
    function addToAnalysis(adNum) {
        if (!adNum) return;
        if (state.analysisQueue.some(a => a.AD_Number === adNum)) {
            showToast(`AD ${adNum} ya está en el análisis.`, 'info');
            return;
        }
        const result = state.results.find(r => r.AD_Number === adNum);
        if (result) {
            state.analysisQueue.push(result);
        } else {
            state.analysisQueue.push({ AD_Number: adNum, Make: '', Model: '', Subject: '' });
        }
        showToast(`AD ${adNum} agregado al análisis`, 'success');
        updateAnalysisBadge();
    }

    function removeFromAnalysis(adNum) {
        state.analysisQueue = state.analysisQueue.filter(a => a.AD_Number !== adNum);
        updateAnalysisBadge();
        renderAnalysisDrawer();
    }

    function updateAnalysisBadge() {
        const count = state.analysisQueue.length;
        if (dom.analysisBadgeBtn) dom.analysisBadgeBtn.style.display = count > 0 ? '' : 'none';
        if (dom.analysisCountBadge) dom.analysisCountBadge.textContent = count;
    }

    function openAnalysisDrawer() {
        renderAnalysisDrawer();
        if (dom.analysisDrawer) dom.analysisDrawer.classList.add('open');
        if (dom.analysisOverlay) dom.analysisOverlay.classList.add('active');
    }

    function closeAnalysisDrawer() {
        if (dom.analysisDrawer) dom.analysisDrawer.classList.remove('open');
        if (dom.analysisOverlay) dom.analysisOverlay.classList.remove('active');
    }

    function renderAnalysisDrawer() {
        if (!dom.analysisDrawerBody) return;
        if (state.analysisQueue.length === 0) {
            dom.analysisDrawerBody.innerHTML = '<p class="analysis-empty">No hay ADs en la cola de análisis.</p>';
            return;
        }
        dom.analysisDrawerBody.innerHTML = state.analysisQueue.map(a =>
            `<div class="analysis-item" data-ad="${esc(a.AD_Number)}">
                <div class="analysis-item-info">
                    <div class="analysis-item-ad">${esc(a.AD_Number)}</div>
                    <div class="analysis-item-meta">${esc(a.Make)}${a.Model ? ' ' + esc(a.Model) : ''}</div>
                </div>
                <button class="analysis-item-remove" data-ad="${esc(a.AD_Number)}" title="Quitar">&times;</button>
            </div>`
        ).join('');
        dom.analysisDrawerBody.querySelectorAll('.analysis-item-remove').forEach(btn => {
            btn.addEventListener('click', () => removeFromAnalysis(btn.dataset.ad));
        });
    }

    if (dom.analysisBadgeBtn) dom.analysisBadgeBtn.addEventListener('click', openAnalysisDrawer);
    if (dom.analysisOverlay) dom.analysisOverlay.addEventListener('click', closeAnalysisDrawer);
    if (dom.analysisDrawerClose) dom.analysisDrawerClose.addEventListener('click', closeAnalysisDrawer);
    if (dom.btnAnalyzeAeris) dom.btnAnalyzeAeris.addEventListener('click', () => {
        closeAnalysisDrawer();
        if (dom.aerisModal) dom.aerisModal.style.display = 'flex';
    });
    if (dom.aerisModalClose) dom.aerisModalClose.addEventListener('click', () => { if (dom.aerisModal) dom.aerisModal.style.display = 'none'; });
    if (dom.aerisModalOk) dom.aerisModalOk.addEventListener('click', () => { if (dom.aerisModal) dom.aerisModal.style.display = 'none'; });
    if (dom.aerisModal) dom.aerisModal.addEventListener('click', (e) => { if (e.target === dom.aerisModal) dom.aerisModal.style.display = 'none'; });

    updateAnalysisBadge();

    // ================================================================
    // PROFILE & POPOVER
    // ================================================================
    (function initProfile() {
        const profileMenuBtn = $('profile-menu-btn');
        const profilePopover = $('profile-popover');
        if (profileMenuBtn && profilePopover) {
            profileMenuBtn.addEventListener('click', function (e) {
                e.stopPropagation();
                profilePopover.classList.toggle('active');
            });
            document.addEventListener('click', function () { profilePopover.classList.remove('active'); });
            profilePopover.addEventListener('click', function (e) { e.stopPropagation(); });
        }
        var accountBtn = $('profile-account');
        if (accountBtn) accountBtn.addEventListener('click', function () {
            showToast('Próximamente', 'info');
            if (profilePopover) profilePopover.classList.remove('active');
        });
        var logoutBtn = $('profile-logout');
        if (logoutBtn) logoutBtn.addEventListener('click', function () {
            fetch('/auth/logout', { method: 'POST' }).then(function () {
                window.location.href = '/';
            }).catch(function () { window.location.href = '/'; });
        });
    })();

    function loadUserProfile() {
        fetch('/auth/me').then(function (r) { return r.json(); }).then(function (data) {
            if (!data.user) return;
            var user = data.user;
            var nameEl = $('profile-name');
            var planEl = $('profile-plan');
            var avatarEl = $('profile-avatar');
            if (nameEl) nameEl.textContent = user.name || user.email || '';
            if (planEl) planEl.textContent = data.plan || 'free';
            if (avatarEl) {
                if (user.picture) {
                    avatarEl.innerHTML = '<img src="' + esc(user.picture) + '" alt="">';
                } else {
                    var raw = user.name || user.email || '';
                    var parts = raw.split(/[\s@]+/).filter(Boolean);
                    var initials = parts.length >= 2
                        ? (parts[0][0] + parts[1][0]).toUpperCase()
                        : raw.slice(0, 2).toUpperCase();
                    avatarEl.textContent = initials;
                }
            }
        }).catch(function () {});
    }

    // ================================================================
    // INIT
    // ================================================================
    renderRecentSearches();
    loadUserProfile();
    fetch('/api/env').then(r => r.json()).then(d => {
        _isServerless = !!d.serverless;
    }).catch(() => {}).finally(() => {
        if (!loadFromUrlParams()) { restoreSession(); }
    });

})();
