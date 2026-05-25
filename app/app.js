
    // Global Error Logger for diagnostics
    window.onerror = function (message, source, lineno, colno, error) {
      // 1. Show global overlay error box
      const errBox = document.createElement('div');
      errBox.style.cssText = 'position: fixed; top: 0; left: 0; right: 0; background: #ef4444; color: white; padding: 16px; font-family: monospace; font-size: 12px; z-index: 99999; text-align: left; box-shadow: 0 4px 15px rgba(0,0,0,0.5);';
      errBox.innerHTML = `<strong>Error de JS Global:</strong> ${message}<br>Fichero: ${source}<br>Línea: ${lineno}:${colno}<br><pre style="margin-top: 8px; font-size: 10px; opacity: 0.9;">${error ? error.stack : ''}</pre>`;
      document.body.appendChild(errBox);

      // 2. Also update floating diagnostic box
      const diagInit = document.getElementById('diagInit');
      if (diagInit) { diagInit.textContent = 'ERROR GLOBAL'; diagInit.style.color = '#ef4444'; }
      const diagErr = document.getElementById('diagErrorBox');
      if (diagErr) {
        diagErr.style.display = 'block';
        diagErr.textContent = `${message}\nLínea: ${lineno}:${colno}\nFichero: ${source}`;
      }

      // 3. Post telemetry back to console
      fetch('/api/log-error', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: "Global: " + message,
          source: source,
          lineno: lineno,
          colno: colno,
          stack: error ? error.stack : ''
        })
      });

      return false;
    };

    // Diagnostic tracker: Script is loaded and executing
    (function() {
      const s = document.getElementById('diagScript');
      if (s) { s.textContent = 'SÍ'; s.style.color = '#10b981'; }
    })();

    async function init() {
      try {
        const diagDOM = document.getElementById('diagDOM');
        if (diagDOM) { diagDOM.textContent = 'SÍ'; diagDOM.style.color = '#10b981'; }
        const diagInit = document.getElementById('diagInit');
        if (diagInit) { diagInit.textContent = 'EJECUTANDO...'; diagInit.style.color = '#f59e0b'; }

        const searchForm = document.getElementById('searchForm');
    const resultBox = document.getElementById('resultBox');
    const searchResults = document.getElementById('searchResults');
    const docList = document.getElementById('docList');
    const markdownViewer = document.getElementById('markdownViewer');
    const fragmentList = document.getElementById('fragmentList');
    const fragmentQuery = document.getElementById('fragmentQuery');
    const fragmentSearch = document.getElementById('fragmentSearch');
    const fragmentClear = document.getElementById('fragmentClear');
    const fragmentPrev = document.getElementById('fragmentPrev');
    const fragmentNext = document.getElementById('fragmentNext');
    const fragmentPageInfo = document.getElementById('fragmentPageInfo');
    const fragCountBadge = document.getElementById('fragCountBadge');
    const openRawMarkdown = document.getElementById('openRawMarkdown');
    const downloadOriginalPDF = document.getElementById('downloadOriginalPDF');
    const refreshDocs = document.getElementById('refreshDocs');
    const viewerTitle = document.getElementById('viewerTitle');
    const viewerMetaText = document.getElementById('viewerMetaText');
    const tabComplete = document.getElementById('tabComplete');
    const tabFragment = document.getElementById('tabFragment');
    const tabPDF = document.getElementById('tabPDF');
    const viewerLoading = document.getElementById('viewerLoading');
    const meiliStatus = document.getElementById('meiliStatus');
    const meiliStatusText = document.getElementById('meiliStatusText');
    const suggestionChips = document.getElementById('suggestionChips');

    // Uploader DOM elements
    const uploadForm = document.getElementById('uploadForm');
    const uploadButton = document.getElementById('uploadButton');
    const uploadStatus = document.getElementById('uploadStatus');
    const dropZone = document.getElementById('dropZone');
    const fileInput = document.getElementById('files');

    let activeDocumentId = null;
    let activeFragment = null;
    let fullMarkdownContent = '';
    let activeSearchQuery = '';
    let currentFragmentPage = 1;
    let currentFragmentPageSize = 5;
    let currentFragmentTotalPages = 1;
    let currentFragmentQuery = '';
    let viewMode = 'complete'; // 'complete' or 'fragment'

    // Configure Marked.js Options
    if (window.marked) {
      marked.setOptions({
        gfm: true,
        breaks: true,
        headerIds: true,
        mangle: false
      });
    }

    // Dynamic Meilisearch status checking
    async function checkMeilisearchStatus() {
      try {
        const res = await fetch('/health?t=' + Date.now());
        const data = await res.json();
        if (data.meilisearch === 'connected') {
          meiliStatus.className = 'status-badge online';
          meiliStatusText.textContent = 'Servidor Conectado';
        } else {
          meiliStatus.className = 'status-badge offline';
          meiliStatusText.textContent = 'Búsqueda Local Activa';
        }
      } catch {
        meiliStatus.className = 'status-badge offline';
        meiliStatusText.textContent = 'Servidor Offline';
      }
    }

    // HTML escape utility
    function escapeHtml(value) {
      return value
        .toString()
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
    }

    // DOM-based Recursive Search Terms Highlighter (Tag Corruption Free!)
    function highlightDOM(element, query) {
      if (!query || !query.trim()) return;
      const terms = query.trim().split(/\s+/).filter(Boolean);
      if (!terms.length) return;

      // Sort terms by length in descending order to match longer phrases first
      const sortedTerms = [...terms].sort((a, b) => b.length - a.length);
      const escapedTerms = sortedTerms.map(term => term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
      const pattern = new RegExp(`(${escapedTerms.join('|')})`, 'gi');

      function walk(node) {
        if (node.nodeType === Node.TEXT_NODE) {
          const text = node.nodeValue;
          if (pattern.test(text)) {
            const parent = node.parentNode;
            if (parent && parent.nodeName === 'MARK') return; // Skip already highlighted
            if (parent && ['SCRIPT', 'STYLE', 'TEXTAREA'].includes(parent.nodeName)) return;

            const tempSpan = document.createElement('span');
            const escapedText = escapeHtml(text);
            tempSpan.innerHTML = escapedText.replace(pattern, '<mark>$1</mark>');
            
            while (tempSpan.firstChild) {
              parent.insertBefore(tempSpan.firstChild, node);
            }
            parent.removeChild(node);
          }
        } else {
          for (let i = node.childNodes.length - 1; i >= 0; i--) {
            walk(node.childNodes[i]);
          }
        }
      }
      walk(element);
    }

    // Prevent default drag/drop behaviors globally to avoid page navigation
    window.addEventListener('dragover', (e) => e.preventDefault());
    window.addEventListener('drop', (e) => e.preventDefault());

    if (dropZone && fileInput && uploadButton) {
      // Trigger file selection dialog on drop-zone click
      dropZone.addEventListener('click', () => fileInput.click());

      // Highlight drop-zone when dragging file over it
      ['dragenter', 'dragover'].forEach(eventName => {
        dropZone.addEventListener(eventName, (e) => {
          e.preventDefault();
          dropZone.classList.add('dragover');
        }, false);
      });

      ['dragleave', 'dragend', 'drop'].forEach(eventName => {
        dropZone.addEventListener(eventName, (e) => {
          e.preventDefault();
          dropZone.classList.remove('dragover');
        }, false);
      });

      // Handle dropped files
      dropZone.addEventListener('drop', (e) => {
        const dt = e.dataTransfer;
        const files = dt.files;
        if (files.length) {
          fileInput.files = files;
          updateDropZoneText(files);
        }
      });

      // Handle manual file selection
      fileInput.addEventListener('change', (e) => {
        if (fileInput.files.length) {
          updateDropZoneText(fileInput.files);
        }
      });

      function updateDropZoneText(files) {
        const names = Array.from(files).map(f => f.name);
        const textSpan = dropZone.querySelector('.drop-zone-text');
        if (names.length === 1) {
          textSpan.innerHTML = `Listo para subir: <strong style="color: #60a5fa;">${escapeHtml(names[0])}</strong>`;
        } else {
          textSpan.innerHTML = `Listo para subir: <strong style="color: #60a5fa;">${names.length} archivos seleccionados</strong>`;
        }
      }

      // Async upload function via Fetch
      async function uploadSelectedFiles() {
        const files = fileInput.files;
        if (!files.length) {
          uploadStatus.textContent = 'Selecciona o arrastra al menos un archivo.';
          uploadStatus.style.color = 'var(--warning)';
          return;
        }

        const formData = new FormData();
        for (const file of files) {
          formData.append('files', file);
        }
        formData.append('index_to_meili', 'true');

        uploadButton.disabled = true;
        uploadStatus.textContent = 'Subiendo y procesando ordenanzas...';
        uploadStatus.style.color = 'var(--primary)';
        
        try {
          const response = await fetch('/api/subida-archivos', {
            method: 'POST',
            headers: {
              'x-requested-with': 'fetch'
            },
            body: formData,
          });

          const rawText = await response.text();
          let data = null;
          try {
            data = JSON.parse(rawText);
          } catch {
            data = { message: rawText };
          }

          if (!response.ok) {
            const detail = data?.detail || data?.message || `Error HTTP ${response.status}`;
            uploadStatus.textContent = `Error: ${detail}`;
            uploadStatus.style.color = '#f87171';
            return;
          }

          const uploaded = (data.files || []).map(item => item.source_file).filter(Boolean);
          uploadStatus.textContent = uploaded.length
            ? `¡Cargado con éxito!: ${uploaded.join(', ')}`
            : '¡Archivos cargados con éxito!';
          uploadStatus.style.color = 'var(--success)';
          
          // Clear files selection in dropzone
          fileInput.value = '';
          const textSpan = dropZone.querySelector('.drop-zone-text');
          textSpan.innerHTML = 'Arrastra archivos aquí o <span class="browse-link">busca en tu PC</span>';
          
          // Refresh document list in library
          await loadDocuments();
        } catch (error) {
          uploadStatus.textContent = `Error de red: ${error.message || error}`;
          uploadStatus.style.color = '#f87171';
        } finally {
          uploadButton.disabled = false;
        }
      }

      uploadButton.addEventListener('click', uploadSelectedFiles);
    }



    // Library rendering
    async function loadDocuments() {
      const refreshIcon = refreshDocs.querySelector('svg');
      if (refreshIcon) refreshIcon.classList.add('rotate-spinner');
      
      const diagLoadDocs = document.getElementById('diagLoadDocs');
      if (diagLoadDocs) { diagLoadDocs.textContent = 'CARGANDO...'; diagLoadDocs.style.color = '#f59e0b'; }
      
      docList.innerHTML = '<p class="description-text" style="text-align: center; font-style: italic;">Actualizando biblioteca...</p>';
      
      try {
        const response = await fetch(`/api/pdfs?t=${Date.now()}`, { cache: 'no-store' });
        const documents = await response.json();

        if (diagLoadDocs) { diagLoadDocs.textContent = 'SÍ'; diagLoadDocs.style.color = '#10b981'; }

        if (!documents.length) {
          docList.innerHTML = '<p class="description-text" style="text-align: center; font-style: italic;">Todavía no hay PDFs subidos.</p>';
          return;
        }

        docList.innerHTML = '';
        documents.forEach((doc) => {
          const button = document.createElement('button');
          button.type = 'button';
          button.className = 'doc-item' + (doc.id === activeDocumentId ? ' active' : '');
          
          const tagClass = doc.converted ? 'ready' : 'pending';
          const tagText = doc.converted ? 'Markdown' : 'Pendiente';
          
          button.innerHTML = `
            <div class="doc-item-title">${escapeHtml(doc.title)}</div>
            <div class="doc-item-meta">
              <span class="indicator-tag ${tagClass}">
                <span class="dot-pulse"></span>
                <span>${tagText}</span>
              </span>
              <span>${escapeHtml(doc.original_filename || doc.filename)}</span>
            </div>
          `;
          
          button.addEventListener('click', () => loadMarkdown(doc.id));
          docList.appendChild(button);
        });
      } catch (error) {
        if (diagLoadDocs) { diagLoadDocs.textContent = 'ERROR: ' + error.message; diagLoadDocs.style.color = '#ef4444'; }
        docList.innerHTML = '<p class="description-text" style="text-align: center; color: var(--warning);">Error al cargar biblioteca.</p>';
      } finally {
        if (refreshIcon) refreshIcon.classList.remove('rotate-spinner');
      }
    }

    // Load Document Content
    async function loadMarkdown(documentId, fragmentHint = null) {
      activeDocumentId = documentId;
      openRawMarkdown.disabled = false;
      downloadOriginalPDF.disabled = false;
      viewerLoading.style.display = 'inline-block';
      markdownViewer.innerHTML = '<p class="description-text" style="text-align: center; font-style: italic;">Descargando contenido del documento...</p>';
      
      try {
        const response = await fetch(`/api/documentos/${encodeURIComponent(documentId)}/markdown?t=${Date.now()}`, { cache: 'no-store' });
        if (!response.ok) {
          markdownViewer.innerHTML = '<p class="description-text" style="text-align: center; color: var(--warning);">No se pudo cargar el documento.</p>';
          return;
        }
        
        const data = await response.json();
        fullMarkdownContent = data.markdown;
        
        // Update viewer headers
        const matchedDoc = Array.from(docList.querySelectorAll('.doc-item')).find(item => item.innerHTML.includes(documentId));
        const cleanTitle = (matchedDoc && matchedDoc.querySelector('.doc-item-title')) ? matchedDoc.querySelector('.doc-item-title').textContent : (documentId.split('_', 1)[1] || documentId);
        viewerTitle.textContent = cleanTitle;
        viewerMetaText.textContent = `Archivo: ${documentId}.md`;
        
        tabFragment.disabled = !fragmentHint;
        tabPDF.disabled = false;
        
        if (fragmentHint) {
          activeFragment = fragmentHint;
          setViewMode('fragment');
        } else {
          activeFragment = null;
          setViewMode('complete');
        }

        // Reset fragment filter inputs when opening a new document
        if (fragmentHint && fragmentHint.chunk_number) {
          currentFragmentQuery = '';
          fragmentQuery.value = '';
        }

        const targetPage = fragmentHint && fragmentHint.chunk_number
          ? Math.max(1, Math.ceil(fragmentHint.chunk_number / currentFragmentPageSize))
          : 1;

        await loadFragments(documentId, targetPage);
        
        // Refresh library highlight state
        docList.querySelectorAll('.doc-item').forEach(item => {
          item.classList.remove('active');
        });
        const activeDocBtn = Array.from(docList.querySelectorAll('.doc-item')).find(btn => btn.innerHTML.includes(documentId));
        if (activeDocBtn) activeDocBtn.classList.add('active');
        
      } catch (error) {
        markdownViewer.innerHTML = '<p class="description-text" style="text-align: center; color: var(--warning);">Ocurrió un error al cargar el Markdown.</p>';
      } finally {
        viewerLoading.style.display = 'none';
      }
    }

    // Set View Mode Tab
    function setViewMode(mode) {
      viewMode = mode;
      if (mode === 'complete') {
        tabComplete.classList.add('active');
        tabFragment.classList.remove('active');
        tabPDF.classList.remove('active');
        
        if (window.marked) {
          markdownViewer.innerHTML = marked.parse(fullMarkdownContent || '*Vacío*');
        } else {
          markdownViewer.innerHTML = `<pre style="white-space: pre-wrap;">${escapeHtml(fullMarkdownContent)}</pre>`;
        }
      } else if (mode === 'fragment' && activeFragment) {
        tabFragment.classList.add('active');
        tabComplete.classList.remove('active');
        tabPDF.classList.remove('active');
        
        const content = activeFragment.content_markdown || activeFragment.content_text || '';
        
        let headerHtml = `<div style="background: rgba(139, 92, 246, 0.05); border: 1px dashed rgba(139, 92, 246, 0.2); border-radius: 12px; padding: 12px 16px; margin-bottom: 16px; font-size: 13px;">
          <span style="font-weight: 700; color: #a78bfa;">Fragmento ${escapeHtml(activeFragment.chunk_number || '?')}</span> · Sección: <strong style="color: #fff;">${escapeHtml(activeFragment.section || 'General')}</strong>
        </div>`;
        
        if (window.marked) {
          markdownViewer.innerHTML = headerHtml + marked.parse(content);
        } else {
          markdownViewer.innerHTML = headerHtml + `<pre style="white-space: pre-wrap;">${escapeHtml(content)}</pre>`;
        }
      } else if (mode === 'pdf') {
        tabPDF.classList.add('active');
        tabComplete.classList.remove('active');
        tabFragment.classList.remove('active');
        
        let pdfUrl = `/api/documentos/${encodeURIComponent(activeDocumentId)}/pdf?t=${Date.now()}`;
        if (activeSearchQuery && activeSearchQuery.trim()) {
          // Strict Adobe PDF open parameters require the word to be enclosed in double quotes: #search="word"
          pdfUrl += `#search=%22${encodeURIComponent(activeSearchQuery.trim())}%22`;
        }
        
        markdownViewer.innerHTML = `
          <div style="display: flex; flex-direction: column; gap: 10px; height: 100%; min-height: 520px;">
            <embed src="${pdfUrl}" type="application/pdf" style="width: 100%; height: 500px; border: none; border-radius: 12px; background: white;"></embed>
            <div style="text-align: center; font-size: 11px; color: var(--text-muted); line-height: 1.4;">
              ¿No se visualiza y se descarga automáticamente? Asegúrate de activar la opción <strong>"Abrir archivos PDF en el navegador"</strong> en la configuración de tu navegador.
            </div>
          </div>
        `;
      }
      
      // Perform DOM highlighting
      if (mode !== 'pdf') {
        highlightDOM(markdownViewer, activeSearchQuery);
      }
    }

    tabComplete.addEventListener('click', () => setViewMode('complete'));
    tabFragment.addEventListener('click', () => setViewMode('fragment'));
    tabPDF.addEventListener('click', () => setViewMode('pdf'));

    // Load Local Document Fragments
    async function loadFragments(documentId, page = 1) {
      currentFragmentPage = page;
      fragmentList.innerHTML = '<p class="description-text" style="text-align: center; font-style: italic;">Descargando fragmentos...</p>';
      
      const params = new URLSearchParams({
        page: String(page),
        page_size: String(currentFragmentPageSize),
      });
      if (currentFragmentQuery.trim()) {
        params.set('q', currentFragmentQuery.trim());
      }

      try {
        const response = await fetch(`/api/documentos/${encodeURIComponent(documentId)}/fragmentos?${params.toString()}&t=${Date.now()}`, { cache: 'no-store' });
        if (!response.ok) {
          fragmentList.innerHTML = '<p class="description-text" style="text-align: center; color: var(--warning);">No se pudieron cargar los fragmentos.</p>';
          return;
        }

        const payload = await response.json();
        const fragments = payload.items || [];
        currentFragmentTotalPages = payload.total_pages || 1;

        fragCountBadge.textContent = payload.total || 0;
        fragmentPageInfo.textContent = payload.total
          ? `Mostrando ${fragments.length} de ${payload.total} · página ${payload.page} de ${payload.total_pages}`
          : 'Ningún fragmento para mostrar.';

        if (!fragments.length) {
          fragmentList.innerHTML = '<p class="description-text" style="text-align: center; font-style: italic; margin-top: 10px;">Sin fragmentos disponibles.</p>';
          return;
        }

        fragmentList.innerHTML = '';
        fragments.forEach((fragment) => {
          const button = document.createElement('button');
          button.type = 'button';
          const isSelected = activeFragment && activeFragment.id === fragment.id;
          button.className = 'fragment-item' + (isSelected ? ' active' : '');
          
          const cleanPreview = (fragment.content_text || '').slice(0, 140).trim();
          const dots = (fragment.content_text || '').length > 140 ? '...' : '';
          
          button.innerHTML = `
            <div style="display: flex; justify-content: space-between; align-items: center; width: 100%;">
              <span class="fragment-item-title">Fragmento ${escapeHtml(fragment.chunk_number)}</span>
              <span class="result-score" style="font-size: 9px; padding: 1px 4px; background: rgba(59, 130, 246, 0.1); color: #60a5fa;">Score: ${escapeHtml(fragment.score || 0)}</span>
            </div>
            <div class="fragment-meta-info" style="font-weight: 500; color: #fff;">${escapeHtml(fragment.section)}</div>
            <div class="fragment-meta-info" style="color: var(--text-muted); font-size: 11px;">${escapeHtml(cleanPreview)}${dots}</div>
          `;
          
          button.addEventListener('click', () => {
            activeFragment = fragment;
            tabFragment.disabled = false;
            setViewMode('fragment');
            // Re-render fragments to update active highlighting
            loadFragments(documentId, currentFragmentPage);
          });
          fragmentList.appendChild(button);
        });

        fragmentPrev.disabled = currentFragmentPage <= 1;
        fragmentNext.disabled = currentFragmentPage >= currentFragmentTotalPages;
      } catch (error) {
        fragmentList.innerHTML = '<p class="description-text" style="text-align: center; color: var(--warning);">Error al sincronizar fragmentos.</p>';
      }
    }



    // Global Search against Meilisearch
    searchForm.addEventListener('submit', async (event) => {
      event.preventDefault();
      const query = document.getElementById('query').value.trim();
      if (!query) {
        searchResults.innerHTML = '<p class="description-text" style="text-align: center; font-style: italic;">Escribe un término de búsqueda.</p>';
        return;
      }

      searchResults.innerHTML = '<p class="description-text" style="text-align: center; font-style: italic;"><span class="loading-pulse" style="margin-right: 6px; vertical-align: middle;"></span> Consultando Meilisearch...</p>';
      
      try {
        const response = await fetch('/api/buscar', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ query, limit: 10 }),
        });
        const data = await response.json();
        const hits = data.results?.hits || [];
        activeSearchQuery = query;
        
        // Re-highlight active document viewer if active
        if (activeDocumentId) {
          setViewMode(viewMode);
        }

        if (!hits.length) {
          searchResults.innerHTML = '<p class="description-text" style="text-align: center; font-style: italic; color: var(--warning);">Ningún resultado encontrado.</p>';
          if (resultBox) resultBox.textContent = JSON.stringify(data, null, 2);
          return;
        }

        if (resultBox) resultBox.textContent = JSON.stringify(data, null, 2);
        searchResults.innerHTML = '';
        
        hits.forEach((hit) => {
          const item = document.createElement('div');
          item.className = 'result-item';
          
          const titleText = hit.title || hit.document_id || 'Documento';
          
          item.innerHTML = `
            <div class="result-item-header">
              <h4>${escapeHtml(titleText)}</h4>
              <span class="result-score">Score: ${escapeHtml(hit.score || 0)}</span>
            </div>
            <div class="result-meta">
              <span>Sección: <strong>${escapeHtml(hit.section || 'General')}</strong></span>
              <span>Archivo: <strong style="color: var(--text);">${escapeHtml(hit.source || '')}</strong></span>
            </div>
            <div class="result-text">${escapeHtml((hit.content_text || '').slice(0, 300))}...</div>
            <button type="button" class="btn-secondary" style="margin-top: 10px; padding: 6px 14px; font-size: 11px;" data-document="${escapeHtml(hit.document_id || '')}">
              Ver fragmento exacto
            </button>
          `;
          
          // Apply DOM highlighting inside the snippet preview text block
          const snippetTextDiv = item.querySelector('.result-text');
          highlightDOM(snippetTextDiv, query);

          const button = item.querySelector('button[data-document]');
          if (button) {
            button.addEventListener('click', () => loadMarkdown(hit.document_id, hit));
          }
          searchResults.appendChild(item);
        });
      } catch (error) {
        searchResults.innerHTML = `<p class="description-text" style="text-align: center; color: var(--warning);">Error: ${error}</p>`;
      }
    });

    // Suggestion chips handler
    suggestionChips.addEventListener('click', (e) => {
      if (e.target.classList.contains('chip')) {
        const queryInput = document.getElementById('query');
        queryInput.value = e.target.textContent;
        searchForm.dispatchEvent(new Event('submit'));
      }
    });

    // Fragment searches / filtering inside viewer
    fragmentSearch.addEventListener('click', async () => {
      if (!activeDocumentId) {
        return;
      }
      currentFragmentQuery = fragmentQuery.value.trim();
      activeFragment = null;
      await loadFragments(activeDocumentId, 1);
    });

    fragmentClear.addEventListener('click', async () => {
      if (!activeDocumentId) {
        return;
      }
      fragmentQuery.value = '';
      currentFragmentQuery = '';
      activeFragment = null;
      await loadFragments(activeDocumentId, 1);
    });

    fragmentPrev.addEventListener('click', async () => {
      if (!activeDocumentId || currentFragmentPage <= 1) {
        return;
      }
      await loadFragments(activeDocumentId, currentFragmentPage - 1);
    });

    fragmentNext.addEventListener('click', async () => {
      if (!activeDocumentId || currentFragmentPage >= currentFragmentTotalPages) {
        return;
      }
      await loadFragments(activeDocumentId, currentFragmentPage + 1);
    });

    openRawMarkdown.addEventListener('click', () => {
      if (!activeDocumentId) {
        return;
      }
      window.open(`/api/documentos/${encodeURIComponent(activeDocumentId)}/markdown-raw`, '_blank', 'noopener,noreferrer');
    });

    downloadOriginalPDF.addEventListener('click', () => {
      if (!activeDocumentId) {
        return;
      }
      window.open(`/api/documentos/${encodeURIComponent(activeDocumentId)}/pdf?download=true`, '_blank', 'noopener,noreferrer');
    });

    refreshDocs.addEventListener('click', loadDocuments);
    
        // Initial triggers
        checkMeilisearchStatus();
        setInterval(checkMeilisearchStatus, 15000); // Check status every 15s
        await loadDocuments();

        // 4. Diagnostic: init completed successfully
        if (diagInit) { diagInit.textContent = 'SÍ'; diagInit.style.color = '#10b981'; }
      } catch (err) {
        console.error("DOM Ready JS Error:", err);
        
        // Update floating diagnostic panel on error
        const diagInitErr = document.getElementById('diagInit');
        if (diagInitErr) { diagInitErr.textContent = 'ERROR'; diagInitErr.style.color = '#ef4444'; }
        const diagErr = document.getElementById('diagErrorBox');
        if (diagErr) {
          diagErr.style.display = 'block';
          diagErr.textContent = `${err.message}\n${err.stack || ''}`;
        }
        
        // Remote error logger for handled exceptions
        fetch('/api/log-error', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            message: "Handled: " + err.message,
            source: "main.py",
            lineno: 0,
            colno: 0,
            stack: err.stack || ''
          })
        });

        const docList = document.getElementById('docList');
        if (docList) {
          docList.innerHTML = `<div style="padding: 12px; color: #f87171; background: rgba(220, 38, 38, 0.1); border: 1px solid rgba(220, 38, 38, 0.2); border-radius: 12px; font-size: 12px; font-family: var(--font-mono); text-align: left;"><strong>Error de JS:</strong> ${err.message}<br><pre style="white-space: pre-wrap; font-size: 10px; margin-top: 8px; color: #fca5a5;">${err.stack}</pre></div>`;
        }
        const meiliStatusText = document.getElementById('meiliStatusText');
        if (meiliStatusText) meiliStatusText.textContent = "Error de JS";
        const meiliStatus = document.getElementById('meiliStatus');
        if (meiliStatus) meiliStatus.className = "status-badge offline";
      }
    }

    // Start the application immediately since the script is at the bottom of the body
    init();
  