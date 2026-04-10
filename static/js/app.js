/**
 * AD Search — Frontend Application
 * SSE streaming, sidebar, card/table view, URL params, keyboard shortcuts,
 * relevance scoring, recent searches, quick filter, sessionStorage.
 *
 * Bug fixes in this version:
 * - Race condition: requestId per search, SSE events ignored if stale
 * - Ctrl+A selects only visible/filtered page results
 * - Pagination reflects filtered count
 * - URL params priority over sessionStorage (no flash)
 * - Render guard: skip identical re-renders
 * - Inverted index for autocomplete performance
 */
(function () {
    'use strict';

    const DEFAULT_TITLE = 'AD Search \u00b7 Airworthiness Directives';
    document.title = DEFAULT_TITLE;

    // ================================================================
    // STATE
    // ================================================================
    let _requestId = 0; // monotonic ID for race-condition guard

    const state = {
        selectedSource: 'federal_register',
        results: [],
        filteredResults: null, // null = no filter
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
        // Render guard
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
        shortcutsModal: $('shortcuts-modal'),
        shortcutsModalClose: $('shortcuts-modal-close'),
        btnShortcutsHelp: $('btn-shortcuts-help'),
    };

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
        state._lastRenderKey = null; // force re-render on mode switch
        if (state.results.length > 0) renderResults();
    }
    if (dom.viewTableBtn) dom.viewTableBtn.addEventListener('click', () => setViewMode('table'));
    if (dom.viewCardsBtn) dom.viewCardsBtn.addEventListener('click', () => setViewMode('cards'));
    setViewMode(state.viewMode);

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

    function applyQuickFilter() {
        if (!state.quickFilter) {
            state.filteredResults = null;
            return;
        }
        const q = state.quickFilter;
        state.filteredResults = state.results.filter(r =>
            (r.AD_Number || '').toLowerCase().includes(q) ||
            (r.Make || '').toLowerCase().includes(q) ||
            (r.Model || '').toLowerCase().includes(q) ||
            (r.Subject || '').toLowerCase().includes(q)
        );
    }

    function getActiveResults() {
        return state.filteredResults !== null ? state.filteredResults : state.results;
    }

    // ================================================================
    // SEARCH — SSE STREAMING (with requestId race guard)
    // ================================================================
    dom.searchForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        if (state.searching) {
            // Abort current search and start new one
            if (state.streamAbort) state.streamAbort.abort();
            state.searching = false;
        }

        const formData = new FormData(dom.searchForm);
        const params = {
            source: state.selectedSource,
            keyword: formData.get('keyword') || '',
            make: formData.get('make') || '',
            model: formData.get('model') || '',
            product_type: formData.get('product_type') || '',
            date_from: formData.get('date_from') || '',
            date_to: formData.get('date_to') || '',
        };

        const hasInput = params.keyword || params.make || params.model;
        if (!hasInput && params.source !== 'all') {
            showToast('Ingresa al menos un termino de busqueda.', 'error');
            return;
        }

        state.lastSearchTerm = params.keyword || params.make || params.model || '';
        state.lastParams = params;
        dismissRestoreBanner();
        closeSidebar();

        // Push URL state
        const qs = new URLSearchParams(params).toString();
        history.pushState(params, '', '/app?' + qs);

        await streamSearch(params);
    });

    async function streamSearch(params) {
        // Generate unique request ID for race-condition guard
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
        state._lastRenderKey = null;
        if (dom.quickFilter) dom.quickFilter.value = '';
        clearSelection();

        showButtonLoading(true);
        setStatus('searching', 'Buscando...');
        updateTitle('searching');

        if (dom.emptyState) dom.emptyState.style.display = 'none';
        dom.resultsSection.style.display = 'block';
        showSkeleton();
        resetSourceCardStates(params.source);

        const t0 = performance.now();
        const qs = new URLSearchParams(params).toString();
        const abortCtrl = new AbortController();
        state.streamAbort = abortCtrl;

        try {
            const response = await fetch(`/api/search/stream?${qs}`, { signal: abortCtrl.signal });
            if (!response.ok) throw new Error(`Error del servidor (${response.status})`);

            // Check if this request is still active
            if (thisRequestId !== _requestId) return;

            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;
                // Race guard: if a newer search started, abandon this one
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
                        // Race guard
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
                        state._lastRenderKey = null; // force render for streaming updates
                        renderResults();
                        updateTitle('results');
                    } catch (_) {}
                }
            }

            // Final race guard
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
        const keys = source === 'all' ? ['federal_register','easa','anac_brazil','transport_canada'] : [source];
        keys.forEach(key => {
            const el = $(`count-${key}`);
            if (el) { el.innerHTML = '<span class="source-card-spinner"></span>'; el.classList.remove('source-count--error'); }
        });
    }
    function updateSourceCard(srcKey, count, error) {
        const el = $(`count-${srcKey}`);
        if (!el) return;
        if (error) { el.textContent = 'Error'; el.classList.add('source-count--error'); }
        else { el.textContent = `${count.toLocaleString('es-ES')} ADs`; el.classList.remove('source-count--error'); }
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
        // If searching, abort
        if (state.streamAbort) { state.streamAbort.abort(); state.streamAbort = null; }
        dom.searchForm.reset();
        state.results = []; state.filteredResults = null; state.currentPage = 1;
        state.activeManufacturerKey = null; state.activeManufacturerModels = null;
        state.lastErrors = []; state.sourceCounts = {}; state.lastParams = null;
        state.quickFilter = ''; state._lastRenderKey = null; state.searching = false;
        if (dom.quickFilter) dom.quickFilter.value = '';
        clearSelection();
        showButtonLoading(false);
        dom.resultsSection.style.display = 'none';
        if (dom.emptyState) dom.emptyState.style.display = 'none';
        if (dom.inputModel) dom.inputModel.placeholder = 'Ej: 737, A320...';
        updateTitle('default');
        document.querySelectorAll('.source-count').forEach(el => { el.innerHTML = '&mdash;'; el.classList.remove('source-count--error'); });
        dismissRestoreBanner();
        history.replaceState(null, '', '/app');
    });

    // ================================================================
    // SELECTION — operates on filtered + paged results only
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

    function getPageIndices() {
        const active = getActiveResults();
        const sorted = getSortedResults(active);
        const start = (state.currentPage - 1) * state.pageSize;
        const end = start + state.pageSize;
        const indices = [];
        for (let i = start; i < end && i < sorted.length; i++) {
            indices.push(state.results.indexOf(sorted[i]));
        }
        return indices;
    }

    function updateSelectAllCheckbox() {
        const pageIndices = getPageIndices();
        const allChecked = pageIndices.length > 0 && pageIndices.every(idx => state.selectedIds.has(idx));
        if (dom.selectAll) dom.selectAll.checked = allChecked;
    }
    if (dom.selectAll) {
        dom.selectAll.addEventListener('change', () => {
            const pageIndices = getPageIndices();
            for (const idx of pageIndices) {
                if (dom.selectAll.checked) state.selectedIds.add(idx); else state.selectedIds.delete(idx);
            }
            state._lastRenderKey = null;
            renderResults(); updateBulkBar();
        });
    }
    if (dom.bulkDeselect) dom.bulkDeselect.addEventListener('click', () => { clearSelection(); state._lastRenderKey = null; renderResults(); });
    if (dom.bulkExportCsv) dom.bulkExportCsv.addEventListener('click', () => exportSelected('csv'));
    if (dom.bulkExportExcel) dom.bulkExportExcel.addEventListener('click', () => exportSelected('excel'));
    if (dom.bulkExportZip) dom.bulkExportZip.addEventListener('click', () => exportSelected('zip'));

    async function exportSelected(format) {
        if (state.selectedIds.size === 0) { showToast('No hay registros seleccionados.', 'error'); return; }
        const selectedResults = [...state.selectedIds].map(idx => state.results[idx]).filter(Boolean);
        try {
            showToast(`Exportando ${format.toUpperCase()}...`, 'info');
            const payload = { results: selectedResults };
            if (format === 'zip' && state.lastParams) payload.filters = state.lastParams;
            const response = await fetch(`/api/export/${format}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) });
            if (!response.ok) throw new Error('Error al exportar');
            const blob = await response.blob();
            downloadBlob(blob, format, selectedResults.length);
        } catch (error) { showToast(`Error: ${error.message}`, 'error'); }
    }

    // ================================================================
    // RENDER (with guard against identical re-renders)
    // ================================================================
    function renderResults() {
        const active = getActiveResults();
        const sorted = getSortedResults(active);
        const totalPages = Math.ceil(sorted.length / state.pageSize);
        const start = (state.currentPage - 1) * state.pageSize;
        const end = start + state.pageSize;
        const pageResults = sorted.slice(start, end);

        // Render guard: build key from page, sort, filter, viewMode, selection count, result count
        const renderKey = `${state.currentPage}|${state.sortField}|${state.sortOrder}|${state.quickFilter}|${state.viewMode}|${state.selectedIds.size}|${active.length}|${state.results.length}`;
        if (state._lastRenderKey === renderKey) return; // skip identical render
        state._lastRenderKey = renderKey;

        // Count display
        if (state.filteredResults !== null) {
            dom.resultsCount.textContent = `Mostrando ${active.length.toLocaleString('es-ES')} de ${state.results.length.toLocaleString('es-ES')}`;
        } else {
            dom.resultsCount.textContent = `${active.length.toLocaleString('es-ES')} resultados`;
        }
        if (dom.resultsTime) dom.resultsTime.textContent = state.lastSearchMs !== null ? `\u00b7 ${(state.lastSearchMs / 1000).toFixed(1)}s` : '';

        if (state.viewMode === 'table') renderTable(pageResults);
        else renderCards(pageResults);

        renderPagination(totalPages);
        updateSortHeaders();
        updateSelectAllCheckbox();
        updateBulkBar();
    }

    function renderTable(pageResults) {
        dom.resultsBody.innerHTML = pageResults.map(r => {
            const idx = state.results.indexOf(r);
            const checked = state.selectedIds.has(idx) ? 'checked' : '';
            const rel = calcRelevance(r);
            return `<tr class="${checked ? 'row-selected' : ''}">
                <td class="td-checkbox"><input type="checkbox" class="row-check" data-idx="${idx}" ${checked}></td>
                <td style="text-align:center">${relevanceBarsHtml(rel)}</td>
                <td>${getSourceBadge(r.Source)}</td>
                <td class="td-ad-number" title="${esc(r.AD_Number)}">${esc(r.AD_Number)}</td>
                <td title="${esc(r.Make)}">${esc(r.Make)}</td>
                <td title="${esc(r.Model)}">${esc(r.Model)}</td>
                <td class="col-tipo">${getTypeBadge(r.Product_Type)}</td>
                <td class="td-subject" title="${esc(r.Subject)}">${formatSubject(r.Subject, 75)}</td>
                <td class="td-date">${formatDate(r.Effective_Date)}</td>
                <td>${pdfCell(r)}</td>
            </tr>`;
        }).join('');
        bindRowChecks();
    }

    function renderCards(pageResults) {
        dom.cardsContainer.innerHTML = pageResults.map(r => {
            const idx = state.results.indexOf(r);
            const checked = state.selectedIds.has(idx) ? 'checked' : '';
            const rel = calcRelevance(r);
            return `<div class="result-card ${checked ? 'row-selected' : ''}">
                <div class="result-card-top">
                    <div class="result-card-badge">${getSourceBadge(r.Source)}</div>
                    <div class="result-card-actions">${relevanceBarsHtml(rel)}<input type="checkbox" class="row-check" data-idx="${idx}" ${checked}></div>
                </div>
                <div class="card-ad-number">${esc(r.AD_Number)}</div>
                <div class="card-make-model">${esc(r.Make)} ${esc(r.Model)} ${getTypeBadge(r.Product_Type)}</div>
                <div class="card-subject">${esc(r.Subject)}</div>
                <div class="result-card-bottom">
                    <span class="card-date">${formatDate(r.Effective_Date)}</span>
                    ${pdfCellCard(r)}
                </div>
            </div>`;
        }).join('');
        bindRowChecks(dom.cardsContainer);
    }

    function bindRowChecks(container) {
        (container || dom.resultsBody).querySelectorAll('.row-check').forEach(cb => {
            cb.addEventListener('change', () => {
                const idx = parseInt(cb.dataset.idx);
                if (cb.checked) state.selectedIds.add(idx); else state.selectedIds.delete(idx);
                const row = cb.closest('tr') || cb.closest('.result-card');
                if (row) row.classList.toggle('row-selected', cb.checked);
                updateSelectAllCheckbox(); updateBulkBar();
            });
        });
    }

    function pdfCell(r) {
        return r.PDF_Link
            ? `<a href="${esc(r.PDF_Link)}" target="_blank" rel="noopener" class="pdf-link"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>Ver</a>`
            : '<span style="color:var(--text-muted)">--</span>';
    }

    function pdfCellCard(r) {
        return r.PDF_Link
            ? `<a href="${esc(r.PDF_Link)}" target="_blank" rel="noopener" class="pdf-link"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>Ver</a>`
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
    if (dom.btnShareUrl) {
        dom.btnShareUrl.addEventListener('click', copySearchUrl);
    }
    function copySearchUrl() {
        navigator.clipboard.writeText(window.location.href).then(() => {
            showToast('URL copiada al portapapeles.', 'success');
        }).catch(() => showToast('No se pudo copiar la URL.', 'error'));
    }

    // ================================================================
    // URL PARAMS — Auto-search on load (priority over sessionStorage)
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
        };

        fillForm(p);
        state.lastParams = p;
        state.lastSearchTerm = p.keyword || p.make || p.model || '';
        streamSearch(p);
        return true;
    }

    window.addEventListener('popstate', (e) => {
        if (e.state && e.state.source) {
            fillForm(e.state);
            state.lastParams = e.state;
            state.lastSearchTerm = e.state.keyword || e.state.make || e.state.model || '';
            streamSearch(e.state);
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
    // RECENT SEARCHES
    // ================================================================
    const RECENT_KEY = 'recentSearches';
    const MAX_RECENT = 5;

    function loadRecent() { try { return JSON.parse(localStorage.getItem(RECENT_KEY)) || []; } catch(_) { return []; } }
    function saveRecent(items) { localStorage.setItem(RECENT_KEY, JSON.stringify(items)); }

    function saveRecentSearch(params, count) {
        const items = loadRecent();
        const label = [params.source !== 'all' ? params.source.replace('_',' ') : 'Todas', params.make, params.model, params.keyword].filter(Boolean).join(' \u00b7 ');
        const entry = { label, params, count, ts: Date.now() };
        const filtered = items.filter(i => i.label !== label);
        filtered.unshift(entry);
        saveRecent(filtered.slice(0, MAX_RECENT));
        renderRecentSearches();
    }

    function renderRecentSearches() {
        if (!dom.recentSearches) return;
        const items = loadRecent();
        if (items.length === 0) { dom.recentSearches.innerHTML = '<div class="recent-empty">Sin busquedas recientes</div>'; return; }
        dom.recentSearches.innerHTML = items.map((item, i) =>
            `<div class="recent-item" data-idx="${i}">
                <svg class="recent-item-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
                <span class="recent-item-text" title="${esc(item.label)}">${esc(item.label)}</span>
                <span class="recent-item-count">${item.count || 0}</span>
                <button class="recent-item-remove" data-idx="${i}" title="Eliminar">&times;</button>
            </div>`
        ).join('');

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
        setVal('input-date-from', p.date_from);
        setVal('input-date-to', p.date_to);
        if (p.source) {
            document.querySelectorAll('.source-card').forEach(c => c.classList.toggle('active', c.dataset.source === p.source));
            state.selectedSource = p.source;
        }
    }

    // ================================================================
    // AUTOCOMPLETADO — Inverted index for performance
    // ================================================================

    // Build inverted index once on load
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

    // Use inverted index for make autocomplete
    const makeAC = createAutocomplete(dom.inputMake, (q) => {
        const ql = q.toLowerCase();
        const hits = _acIndex.make.get(ql.substring(0, Math.min(ql.length, 8)));
        if (hits) {
            // Filter to those that actually contain the full query
            const unique = [...new Set(hits)].filter(k => k.toLowerCase().includes(ql));
            return unique.slice(0, 6).map(k => ({label:k, value:k}));
        }
        // Fallback
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
    });

    if (dom.inputMake) dom.inputMake.addEventListener('input', () => {
        const val = dom.inputMake.value.trim();
        if (!val) { state.activeManufacturerKey=null;state.activeManufacturerModels=null;updateModelPlaceholder();return; }
        const found=findManufacturer(val);state.activeManufacturerKey=found?found.key:null;state.activeManufacturerModels=found?found.entry.models:null;updateModelPlaceholder();
    });

    // Use inverted index for model autocomplete
    const modelAC = createAutocomplete(dom.inputModel, (q) => {
        const ql = q.toLowerCase();
        const pool = state.activeManufacturerModels;
        if (pool) {
            // Manufacturer-scoped: filter models directly (small array)
            return pool.filter(m => m.toLowerCase().includes(ql)).slice(0,6).map(m => ({label:m,value:m}));
        }
        // Global: use inverted index
        const hits = _acIndex.model.get(ql.substring(0, Math.min(ql.length, 8)));
        if (hits) {
            const unique = [];
            const seen = new Set();
            for (const h of hits) {
                if (h.model.toLowerCase().includes(ql) && !seen.has(h.model)) {
                    seen.add(h.model);
                    unique.push(h.model);
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
        // Ctrl+Enter: search
        if (e.key === 'Enter' && e.ctrlKey) { dom.searchForm.dispatchEvent(new Event('submit')); return; }
        // Ctrl+Shift+C: copy URL
        if (e.key === 'C' && e.ctrlKey && e.shiftKey) { e.preventDefault(); copySearchUrl(); return; }
        // Ctrl+A: select all on page (only filtered/visible)
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
    function updateTitle(mode) {
        if (mode === 'searching') document.title = 'Buscando... \u2014 AD Search';
        else if (mode === 'results' && state.lastResultCount > 0) document.title = `${state.lastResultCount.toLocaleString('es-ES')} ADs \u2014 AD Search`;
        else document.title = DEFAULT_TITLE;
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
        if (!str) return '\u2014';
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
    // INIT
    // ================================================================
    renderRecentSearches();

    // Priority: URL params > session restore (no flash)
    if (!loadFromUrlParams()) {
        restoreSession();
    }

})();
