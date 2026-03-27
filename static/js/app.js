/**
 * AD Scraper - Frontend Application
 * Handles search, results display, sorting, pagination, and exports.
 */

(function () {
    'use strict';

    // --- State ---
    const state = {
        selectedSource: 'federal_register',
        results: [],
        sortField: null,
        sortOrder: 'asc',
        currentPage: 1,
        pageSize: 25,
        searching: false,
    };

    // --- DOM References ---
    const dom = {
        searchForm: document.getElementById('search-form'),
        btnSearch: document.getElementById('btn-search'),
        btnClear: document.getElementById('btn-clear'),
        resultsSection: document.getElementById('results-section'),
        resultsBody: document.getElementById('results-body'),
        resultsCount: document.getElementById('results-count'),
        pagination: document.getElementById('pagination'),
        loadingOverlay: document.getElementById('loading-overlay'),
        loadingText: document.getElementById('loading-text'),
        statusIndicator: document.getElementById('status-indicator'),
        statusText: document.getElementById('status-text'),
        btnExportCsv: document.getElementById('btn-export-csv'),
        btnExportExcel: document.getElementById('btn-export-excel'),
        toastContainer: document.getElementById('toast-container'),
        themeToggle: document.getElementById('theme-toggle'),
    };

    // --- Theme Selection ---
    const currentTheme = localStorage.getItem('theme') || 'dark';
    if (currentTheme === 'light') {
        document.documentElement.classList.add('light-mode');
    }

    if (dom.themeToggle) {
        dom.themeToggle.addEventListener('click', () => {
            const isLight = document.documentElement.classList.toggle('light-mode');
            localStorage.setItem('theme', isLight ? 'light' : 'dark');
        });
    }

    // --- Source Selection ---
    document.querySelectorAll('.source-card').forEach(card => {
        card.addEventListener('click', () => {
            document.querySelectorAll('.source-card').forEach(c => c.classList.remove('active'));
            card.classList.add('active');
            state.selectedSource = card.dataset.source;
        });
    });

    // --- Search ---
    dom.searchForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        if (state.searching) return;

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

        // Validate at least one field
        const hasInput = params.keyword || params.make || params.model;
        if (!hasInput && params.source !== 'all') {
            showToast('Ingresa al menos un termino de busqueda.', 'error');
            return;
        }

        await performSearch(params);
    });

    async function performSearch(params) {
        state.searching = true;
        state.currentPage = 1;
        state.sortField = null;
        state.sortOrder = 'asc';

        showLoading('Buscando Airworthiness Directives...');
        setStatus('searching', 'Buscando...');
        dom.btnSearch.disabled = true;

        try {
            const response = await fetch('/api/search', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(params),
            });

            if (!response.ok) {
                const errData = await response.json().catch(() => ({}));
                throw new Error(errData.error || `Error del servidor (${response.status})`);
            }

            const data = await response.json();
            state.results = data.results || [];

            if (state.results.length > 0) {
                showToast(`Se encontraron ${state.results.length} resultados.`, 'success');
                renderResults();
                dom.resultsSection.style.display = 'block';
            } else {
                showToast('No se encontraron resultados.', 'info');
                dom.resultsSection.style.display = 'none';
            }

            setStatus('ready', 'Listo');
        } catch (error) {
            console.error('Search error:', error);
            showToast(`Error: ${error.message}`, 'error');
            setStatus('error', 'Error');
        } finally {
            state.searching = false;
            hideLoading();
            dom.btnSearch.disabled = false;
        }
    }

    // --- Clear Form ---
    dom.btnClear.addEventListener('click', () => {
        dom.searchForm.reset();
        state.results = [];
        state.currentPage = 1;
        dom.resultsSection.style.display = 'none';
    });

    // --- Render Results ---
    function renderResults() {
        const sorted = getSortedResults();
        const totalPages = Math.ceil(sorted.length / state.pageSize);
        const start = (state.currentPage - 1) * state.pageSize;
        const end = start + state.pageSize;
        const pageResults = sorted.slice(start, end);

        dom.resultsCount.textContent = `${sorted.length} registros`;

        // Render table rows
        dom.resultsBody.innerHTML = pageResults.map(r => {
            const sourceBadge = getSourceBadge(r.Source);
            const pdfCell = r.PDF_Link
                ? `<a href="${escapeHtml(r.PDF_Link)}" target="_blank" rel="noopener" class="pdf-link">
                     <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
                     Ver
                   </a>`
                : '<span style="color: var(--text-muted);">--</span>';

            return `<tr>
                <td>${sourceBadge}</td>
                <td title="${escapeHtml(r.AD_Number)}">${escapeHtml(r.AD_Number)}</td>
                <td title="${escapeHtml(r.Make)}">${escapeHtml(r.Make)}</td>
                <td title="${escapeHtml(r.Model)}">${escapeHtml(r.Model)}</td>
                <td title="${escapeHtml(r.Subject)}">${escapeHtml(truncate(r.Subject, 80))}</td>
                <td>${escapeHtml(r.Effective_Date)}</td>
                <td>${pdfCell}</td>
            </tr>`;
        }).join('');

        renderPagination(totalPages);
        updateSortHeaders();
    }

    function getSourceBadge(source) {
        if (source.includes('Federal') || source.includes('FAA')) {
            return '<span class="source-badge badge-fr">FAA</span>';
        }
        if (source.includes('EASA')) {
            return '<span class="source-badge badge-easa">EASA</span>';
        }
        if (source.includes('Canada') || source.includes('TC')) {
            return '<span class="source-badge badge-tc">TC</span>';
        }
        if (source.includes('ANAC') || source.includes('Brasil')) {
            return '<span class="source-badge badge-anac">ANAC</span>';
        }
        return `<span class="source-badge">${escapeHtml(source)}</span>`;
    }

    // --- Sorting ---
    document.querySelectorAll('.results-table th.sortable').forEach(th => {
        th.addEventListener('click', () => {
            const field = th.dataset.sort;
            if (state.sortField === field) {
                state.sortOrder = state.sortOrder === 'asc' ? 'desc' : 'asc';
            } else {
                state.sortField = field;
                state.sortOrder = 'asc';
            }
            state.currentPage = 1;
            renderResults();
        });
    });

    function getSortedResults() {
        if (!state.sortField) return [...state.results];

        return [...state.results].sort((a, b) => {
            const valA = (a[state.sortField] || '').toString().toLowerCase();
            const valB = (b[state.sortField] || '').toString().toLowerCase();
            const cmp = valA.localeCompare(valB);
            return state.sortOrder === 'asc' ? cmp : -cmp;
        });
    }

    function updateSortHeaders() {
        document.querySelectorAll('.results-table th.sortable').forEach(th => {
            th.classList.remove('asc', 'desc');
            if (th.dataset.sort === state.sortField) {
                th.classList.add(state.sortOrder);
            }
        });
    }

    // --- Pagination ---
    function renderPagination(totalPages) {
        if (totalPages <= 1) {
            dom.pagination.innerHTML = '';
            return;
        }

        let html = '';

        // Prev
        html += `<button ${state.currentPage === 1 ? 'disabled' : ''} data-page="${state.currentPage - 1}">&lt;</button>`;

        // Page numbers
        const maxVisible = 7;
        let startPage = Math.max(1, state.currentPage - Math.floor(maxVisible / 2));
        let endPage = Math.min(totalPages, startPage + maxVisible - 1);
        if (endPage - startPage < maxVisible - 1) {
            startPage = Math.max(1, endPage - maxVisible + 1);
        }

        if (startPage > 1) {
            html += `<button data-page="1">1</button>`;
            if (startPage > 2) html += `<button disabled>...</button>`;
        }

        for (let i = startPage; i <= endPage; i++) {
            html += `<button data-page="${i}" class="${i === state.currentPage ? 'active' : ''}">${i}</button>`;
        }

        if (endPage < totalPages) {
            if (endPage < totalPages - 1) html += `<button disabled>...</button>`;
            html += `<button data-page="${totalPages}">${totalPages}</button>`;
        }

        // Next
        html += `<button ${state.currentPage === totalPages ? 'disabled' : ''} data-page="${state.currentPage + 1}">&gt;</button>`;

        dom.pagination.innerHTML = html;

        dom.pagination.querySelectorAll('button[data-page]').forEach(btn => {
            btn.addEventListener('click', () => {
                const page = parseInt(btn.dataset.page);
                if (page >= 1 && page <= totalPages) {
                    state.currentPage = page;
                    renderResults();
                    document.getElementById('results-section').scrollIntoView({ behavior: 'smooth', block: 'start' });
                }
            });
        });
    }

    // --- Export ---
    dom.btnExportCsv.addEventListener('click', () => exportResults('csv'));
    dom.btnExportExcel.addEventListener('click', () => exportResults('excel'));

    async function exportResults(format) {
        if (state.results.length === 0) {
            showToast('No hay resultados para exportar.', 'error');
            return;
        }

        try {
            showToast(`Exportando ${format.toUpperCase()}...`, 'info');

            const response = await fetch(`/api/export/${format}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ results: state.results }),
            });

            if (!response.ok) throw new Error('Error al exportar');

            const blob = await response.blob();
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;

            const ext = format === 'excel' ? 'xlsx' : 'csv';
            const timestamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
            a.download = `ad_results_${timestamp}.${ext}`;

            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);

            showToast(`Archivo ${ext.toUpperCase()} descargado.`, 'success');
        } catch (error) {
            console.error('Export error:', error);
            showToast(`Error al exportar: ${error.message}`, 'error');
        }
    }

    // --- Loading ---
    function showLoading(text) {
        dom.loadingText.textContent = text;
        dom.loadingOverlay.style.display = 'flex';
    }

    function hideLoading() {
        dom.loadingOverlay.style.display = 'none';
    }

    // --- Status ---
    function setStatus(type, text) {
        dom.statusText.textContent = text;
        dom.statusIndicator.className = 'status-dot';
        if (type === 'searching') dom.statusIndicator.classList.add('searching');
        if (type === 'error') dom.statusIndicator.classList.add('error');
    }

    // --- Toast Notifications ---
    function showToast(message, type = 'info') {
        const toast = document.createElement('div');
        toast.className = `toast toast-${type}`;

        const iconSvg = {
            success: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 6L9 17l-5-5"/></svg>',
            error: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>',
            info: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4"/><path d="M12 8h.01"/></svg>',
        };

        toast.innerHTML = `
            <div class="toast-icon">${iconSvg[type] || iconSvg.info}</div>
            <span class="toast-message">${escapeHtml(message)}</span>
        `;

        dom.toastContainer.appendChild(toast);

        setTimeout(() => {
            toast.classList.add('removing');
            setTimeout(() => toast.remove(), 250);
        }, 4000);
    }

    // --- Utilities ---
    function escapeHtml(str) {
        if (!str) return '';
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }

    function truncate(str, maxLen) {
        if (!str) return '';
        return str.length > maxLen ? str.slice(0, maxLen) + '...' : str;
    }

    // --- Keyboard shortcut ---
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && e.ctrlKey) {
            dom.searchForm.dispatchEvent(new Event('submit'));
        }
    });

})();
