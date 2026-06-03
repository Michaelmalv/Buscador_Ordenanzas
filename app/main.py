from __future__ import annotations

import html
import re
import shutil
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from app.config import settings
from app.models import SearchRequest
from ingestion.meilisearch_client import MeiliService
from ingestion.markdown_chunker import markdown_to_plain_text, split_markdown_by_headers
from ingestion.workflow import process_uploaded_file

BASE_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE_DIR / 'data' / 'uploads'
MARKDOWN_DIR = BASE_DIR / 'data' / 'markdown'
JSON_DIR = BASE_DIR / 'data' / 'json'
SUPPORTED_EXTENSIONS = {'.pdf', '.doc', '.docx'}

app = FastAPI(title=settings.app_name)


def humanize_stem(stem: str) -> str:
    return stem.replace('_', ' ').replace('-', ' ').strip().title() or 'Documento'


def display_name_from_upload(upload_filename: str) -> str:
    stem = Path(upload_filename).stem
    parts = stem.split('_', 1)
    # Only strip prefix if it is a 32-character hexadecimal UUID
    if len(parts) > 1 and len(parts[0]) == 32 and all(c in '0123456789abcdefABCDEF' for c in parts[0]):
        original_stem = parts[1]
    else:
        original_stem = stem
    return humanize_stem(original_stem)


def sanitize_filename(filename: str) -> str:
    import unicodedata
    normalized = unicodedata.normalize('NFKD', filename)
    ascii_str = normalized.encode('ascii', 'ignore').decode('utf-8')
    sanitized = re.sub(r'[^a-zA-Z0-9-._]', '_', ascii_str)
    sanitized = re.sub(r'_{2,}', '_', sanitized)
    return sanitized


def sync_manual_uploads() -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    MARKDOWN_DIR.mkdir(parents=True, exist_ok=True)
    JSON_DIR.mkdir(parents=True, exist_ok=True)

    for item in list(UPLOAD_DIR.iterdir()):
        if not item.is_file():
            continue
        
        suffix = item.suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            continue
            
        original_name = item.name
        sanitized_name = sanitize_filename(original_name)
        
        target_path = item
        if original_name != sanitized_name:
            target_path = UPLOAD_DIR / sanitized_name
            try:
                item.rename(target_path)
            except Exception as e:
                print(f"Error renaming {original_name} to {sanitized_name}: {e}")
                target_path = item # Fallback
                
        markdown_file = MARKDOWN_DIR / f'{target_path.stem}.md'
        json_file = JSON_DIR / f'{target_path.stem}.json'
        
        if not markdown_file.exists() or not json_file.exists():
            print(f"[!] Archivo manual pendiente de procesar: {target_path.name}. Por favor ejecuta en tu terminal: 'python scripts/procesar_biblioteca.py'")



def list_markdown_documents() -> list[dict]:
    MARKDOWN_DIR.mkdir(parents=True, exist_ok=True)
    documents: list[dict] = []

    for markdown_file in sorted(MARKDOWN_DIR.glob('*.md')):
        documents.append(
            {
                'id': markdown_file.stem,
                'title': humanize_stem(markdown_file.stem),
                'filename': markdown_file.name,
            }
        )

    return documents


def list_uploaded_documents() -> list[dict]:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    
    # Process new manual uploads before listing
    sync_manual_uploads()
    
    documents: list[dict] = []

    for uploaded_file in sorted(UPLOAD_DIR.iterdir()):
        if not uploaded_file.is_file():
            continue

        markdown_file = MARKDOWN_DIR / f'{uploaded_file.stem}.md'
        
        # Calculate clean original filename display
        parts = uploaded_file.name.split('_', 1)
        if len(parts) > 1 and len(parts[0]) == 32 and all(c in '0123456789abcdefABCDEF' for c in parts[0]):
            original_filename = parts[1]
        else:
            original_filename = uploaded_file.name

        documents.append(
            {
                'id': uploaded_file.stem,
                'title': display_name_from_upload(uploaded_file.name),
                'filename': uploaded_file.name,
                'original_filename': original_filename,
                'converted': markdown_file.exists(),
            }
        )

    return documents


def read_markdown_document(document_id: str) -> str:
    markdown_path = MARKDOWN_DIR / f'{document_id}.md'
    if not markdown_path.exists():
        raise FileNotFoundError(document_id)
    return markdown_path.read_text(encoding='utf-8')


SPANISH_STOP_WORDS = {
    'de', 'la', 'el', 'en', 'y', 'a', 'los', 'las', 'un', 'una', 
    'con', 'por', 'para', 'o', 'del', 'al', 'que', 'se', 'su', 'sus',
    'lo', 'como', 'más', 'pero', 'este', 'esta', 'estos', 'estas'
}


def parse_search_query(query_str: str) -> list[dict]:
    import re
    query_str = re.sub(r'\s+', ' ', query_str.strip().lower())
    if not query_str:
        return []

    # Find quoted phrases
    quoted_parts = re.findall(r'"([^"]+)"', query_str)

    # Remove quoted parts to find the rest
    remaining = query_str
    for part in quoted_parts:
        remaining = remaining.replace(f'"{part}"', ' ')

    words = [w.strip() for w in remaining.split() if w.strip()]

    parsed = []
    # Add quoted phrases (required)
    for part in quoted_parts:
        if part.strip():
            parsed.append({
                'text': part.strip(),
                'is_phrase': True,
                'required': True,
                'weight': 1000
            })

    # Check if there are any non-stopwords
    has_non_stopword = any(
        (w not in SPANISH_STOP_WORDS) for w in (words + quoted_parts)
    )

    # Add individual words (required)
    for w in words:
        if has_non_stopword and w in SPANISH_STOP_WORDS:
            continue
        parsed.append({
            'text': w,
            'is_phrase': False,
            'required': True,
            'weight': 10
        })

    # If the original query has no quotes and has multiple words,
    # add the entire query as a boost phrase (optional/not required)
    if not quoted_parts and len(words) > 1:
        parsed.append({
            'text': query_str,
            'is_phrase': True,
            'required': False,
            'weight': 1000
        })

    return parsed


def score_fragment_parsed(haystack: str, parsed_query: list[dict]) -> int:
    if not parsed_query:
        return 0
    score = 0
    for term in parsed_query:
        count = haystack.count(term['text'])
        score += count * term['weight']
    return score


def score_fragment(fragment: dict, fragment_query: str | None = None) -> int:
    normalized_query = fragment_query.strip().lower() if fragment_query else ''
    if not normalized_query:
        return 0

    haystack = f"{fragment['section']} {fragment['content_text']}".lower()
    parsed_query = parse_search_query(normalized_query)
    return score_fragment_parsed(haystack, parsed_query)


def build_fragments(document_id: str, fragment_query: str | None = None) -> list[dict]:
    markdown = read_markdown_document(document_id)
    fragments: list[dict] = []
    normalized_query = fragment_query.strip().lower() if fragment_query else ''
    
    parsed_query = parse_search_query(normalized_query) if normalized_query else []

    for index, chunk in enumerate(split_markdown_by_headers(markdown), start=1):
        content_markdown = chunk['content_markdown'].strip()
        if not content_markdown:
            continue

        content_text = markdown_to_plain_text(content_markdown)
        haystack = f"{chunk['section']} {content_text}".lower()

        # Local filtering: Check if all REQUIRED terms are present
        if parsed_query:
            matched_all = True
            for term in parsed_query:
                if term['required']:
                    if term['text'] not in haystack:
                        matched_all = False
                        break
            if not matched_all:
                continue

        fragments.append(
            {
                'id': f'{document_id}-fragment-{index}',
                'document_id': document_id,
                'chunk_number': index,
                'section': chunk['section'],
                'content_markdown': content_markdown,
                'content_text': content_text,
                'char_count': len(content_markdown),
                'score': score_fragment_parsed(haystack, parsed_query),
            }
        )

    if normalized_query:
        fragments.sort(key=lambda item: (-item['score'], item['chunk_number']))

    return fragments


def paginate_fragments(fragments: list[dict], page: int, page_size: int) -> dict:
    total = len(fragments)
    total_pages = max(1, (total + page_size - 1) // page_size)
    current_page = min(page, total_pages)
    start = (current_page - 1) * page_size
    end = start + page_size

    return {
        'items': fragments[start:end],
        'page': current_page,
        'page_size': page_size,
        'total': total,
        'total_pages': total_pages,
    }


@app.get('/health')
def health() -> dict[str, str]:
    meili_connected = False
    try:
        service = MeiliService.from_settings()
        health_status = service.client.health()
        # In python-meilisearch, health() returns a dict like {'status': 'available'}
        if health_status.get('status') in ('available', 'ok'):
            meili_connected = True
    except Exception:
        pass
    return {
        'status': 'ok',
        'meilisearch': 'connected' if meili_connected else 'disconnected'
    }


@app.get('/', response_class=HTMLResponse)
@app.get('/subida-archivos', response_class=HTMLResponse)
def home(request: Request) -> HTMLResponse:
  upload_message = ''
  if request.query_params.get('upload') == 'success':
    uploaded_files = request.query_params.get('files', '')
    upload_message = (
      'Cargado con éxito'
      + (f': {html.escape(uploaded_files)}' if uploaded_files else '.')
    )

  initial_documents = list_uploaded_documents()
  initial_documents_html = ''
  for document in initial_documents:
    status_text = 'Markdown generado' if document['converted'] else 'Pendiente de conversión'
    initial_documents_html += (
      f'<button type="button" class="doc-item">'
      f'<strong>{html.escape(document["title"])}<br>'
      f'</strong><span class="muted">{html.escape(document["original_filename"] or document["filename"])}'
      f'</span><br><span class="fragment-meta">{status_text}</span>'
      f'</button>'
    )

  return HTMLResponse(
    content='''
<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Busca Ordenanza â NotebookLM Premium</title>
  
  <script>
    window.onerror = function (message, source, lineno, colno, error) {
      fetch('/api/log-error', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: message,
          source: source,
          lineno: lineno,
          colno: colno,
          stack: error ? error.stack : ''
        })
      });
      return false;
    };
  </script>
  
  <!-- Fonts -->
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Outfit:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
  
  <!-- Marked Markdown Parser -->
  <script src="/api/marked.min.js"></script>

  <style>
    :root {
      --primary: #3b82f6;
      --primary-glow: rgba(59, 130, 246, 0.45);
      --accent: #8b5cf6;
      --accent-glow: rgba(139, 92, 246, 0.4);
      --success: #10b981;
      --success-glow: rgba(16, 185, 129, 0.4);
      --warning: #f59e0b;
      --warning-glow: rgba(245, 158, 11, 0.4);
      --bg: #030712;
      --card-bg: rgba(17, 24, 39, 0.65);
      --card-hover: rgba(31, 41, 55, 0.75);
      --border: rgba(255, 255, 255, 0.08);
      --text: #f3f4f6;
      --text-muted: #9ca3af;
      --font-outfit: 'Outfit', sans-serif;
      --font-inter: 'Inter', sans-serif;
      --font-mono: 'JetBrains Mono', monospace;
    }

    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }

    body {
      background: radial-gradient(circle at 10% 20%, rgba(59, 130, 246, 0.07) 0%, transparent 45%),
                  radial-gradient(circle at 90% 80%, rgba(139, 92, 246, 0.07) 0%, transparent 45%),
                  var(--bg);
      color: var(--text);
      font-family: var(--font-inter);
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      overflow-x: hidden;
      line-height: 1.5;
    }

    /* Custom scrollbars */
    ::-webkit-scrollbar {
      width: 8px;
      height: 8px;
    }
    ::-webkit-scrollbar-track {
      background: rgba(255, 255, 255, 0.02);
      border-radius: 999px;
    }
    ::-webkit-scrollbar-thumb {
      background: rgba(255, 255, 255, 0.15);
      border-radius: 999px;
      border: 2px solid transparent;
      background-clip: padding-box;
    }
    ::-webkit-scrollbar-thumb:hover {
      background: rgba(255, 255, 255, 0.3);
      border: 2px solid transparent;
      background-clip: padding-box;
    }

    /* Premium Header */
    header {
      padding: 20px 40px;
      background: rgba(3, 7, 18, 0.8);
      backdrop-filter: blur(12px);
      border-bottom: 1px solid var(--border);
      display: flex;
      justify-content: space-between;
      align-items: center;
      position: sticky;
      top: 0;
      z-index: 100;
      box-shadow: 0 4px 30px rgba(0, 0, 0, 0.3);
    }

    .logo-container {
      display: flex;
      flex-direction: column;
    }

    header h1 {
      font-family: var(--font-outfit);
      font-weight: 800;
      font-size: 26px;
      background: linear-gradient(135deg, #60a5fa, #c084fc, #22d3ee);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      letter-spacing: -0.5px;
      display: flex;
      align-items: center;
      gap: 10px;
    }

    header h1::before {
      content: '';
      display: inline-block;
      width: 12px;
      height: 12px;
      background: linear-gradient(135deg, var(--primary), var(--accent));
      border-radius: 50%;
      box-shadow: 0 0 15px var(--primary-glow);
    }

    header p {
      color: var(--text-muted);
      font-size: 13px;
      margin-top: 2px;
    }

    /* Connection Status Badge */
    .status-badge {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 8px 14px;
      border-radius: 20px;
      background: rgba(255, 255, 255, 0.03);
      border: 1px solid var(--border);
      font-size: 12px;
      font-weight: 600;
      font-family: var(--font-outfit);
      transition: all 0.3s ease;
    }

    .status-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: #9ca3af;
      box-shadow: 0 0 8px rgba(156, 163, 175, 0.3);
    }

    .status-badge.online .status-dot {
      background: var(--success);
      box-shadow: 0 0 12px var(--success-glow);
      animation: pulseGreen 2s infinite;
    }

    .status-badge.offline .status-dot {
      background: var(--warning);
      box-shadow: 0 0 12px var(--warning-glow);
      animation: pulseOrange 2s infinite;
    }

    @keyframes pulseGreen {
      0% { box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7); }
      70% { box-shadow: 0 0 0 8px rgba(16, 185, 129, 0); }
      100% { box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }
    }

    @keyframes pulseOrange {
      0% { box-shadow: 0 0 0 0 rgba(245, 158, 11, 0.7); }
      70% { box-shadow: 0 0 0 8px rgba(245, 158, 11, 0); }
      100% { box-shadow: 0 0 0 0 rgba(245, 158, 11, 0); }
    }

    /* Main Layout */
    main {
      flex: 1;
      padding: 30px 40px;
      display: grid;
      grid-template-columns: 340px 1.1fr 1.3fr;
      gap: 24px;
      align-items: stretch;
      max-width: 1800px;
      margin: 0 auto;
      width: 100%;
    }

    @media (max-width: 1400px) {
      main {
        grid-template-columns: 320px 1fr;
      }
      .col-viewer {
        grid-column: span 2;
      }
    }

    @media (max-width: 992px) {
      main {
        grid-template-columns: 1fr;
      }
      .col-viewer, .col-sidebar {
        grid-column: span 1;
      }
    }

    .col {
      display: flex;
      flex-direction: column;
      gap: 24px;
      min-width: 0;
    }

    /* Glassmorphism Cards */
    .card {
      background: var(--card-bg);
      backdrop-filter: blur(20px);
      border: 1px solid var(--border);
      border-radius: 20px;
      padding: 24px;
      box-shadow: 0 15px 35px -5px rgba(0, 0, 0, 0.5);
      display: flex;
      flex-direction: column;
      transition: transform 0.3s ease, border-color 0.3s ease;
      position: relative;
      overflow: hidden;
    }

    .card::before {
      content: '';
      position: absolute;
      top: 0;
      left: 0;
      right: 0;
      height: 1px;
      background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.08), transparent);
    }

    .card-header-area {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 16px;
    }

    .card-title {
      font-family: var(--font-outfit);
      font-weight: 700;
      font-size: 20px;
      color: #fff;
      display: flex;
      align-items: center;
      gap: 10px;
    }

    .pill-badge {
      display: inline-block;
      padding: 5px 10px;
      border-radius: 20px;
      background: rgba(59, 130, 246, 0.1);
      border: 1px solid rgba(59, 130, 246, 0.15);
      color: #60a5fa;
      font-size: 11px;
      font-weight: 600;
      font-family: var(--font-outfit);
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }

    .description-text {
      color: var(--text-muted);
      font-size: 13px;
      margin-bottom: 18px;
    }

    /* Drag & Drop Zone */
    .drop-zone {
      border: 2px dashed rgba(255, 255, 255, 0.15);
      border-radius: 16px;
      padding: 30px 20px;
      text-align: center;
      background: rgba(255, 255, 255, 0.01);
      cursor: pointer;
      transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 12px;
      margin-bottom: 16px;
      position: relative;
    }

    .drop-zone:hover, .drop-zone.dragover {
      border-color: var(--primary);
      background: rgba(59, 130, 246, 0.03);
      box-shadow: 0 0 20px rgba(59, 130, 246, 0.1) inset;
    }

    .drop-zone-icon {
      width: 44px;
      height: 44px;
      color: var(--text-muted);
      transition: all 0.3s ease;
      background: rgba(255, 255, 255, 0.03);
      padding: 10px;
      border-radius: 12px;
      border: 1px solid var(--border);
    }

    .drop-zone:hover .drop-zone-icon, .drop-zone.dragover .drop-zone-icon {
      color: #60a5fa;
      background: rgba(59, 130, 246, 0.1);
      border-color: rgba(59, 130, 246, 0.2);
      transform: translateY(-2px);
    }

    .drop-zone-text {
      font-size: 13px;
      font-weight: 500;
      color: var(--text);
    }

    .drop-zone-text .browse-link {
      color: #60a5fa;
      font-weight: 600;
      text-decoration: underline;
    }

    .drop-zone-hint {
      font-size: 11px;
      color: var(--text-muted);
    }

    /* Buttons */
    button {
      padding: 12px 20px;
      border-radius: 12px;
      border: none;
      font-family: var(--font-outfit);
      font-weight: 700;
      font-size: 14px;
      cursor: pointer;
      transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
    }

    button.btn-primary {
      background: linear-gradient(135deg, var(--primary), var(--accent));
      color: #fff;
      box-shadow: 0 4px 15px rgba(59, 130, 246, 0.3);
    }

    button.btn-primary:hover:not(:disabled) {
      opacity: 0.95;
      transform: translateY(-1px);
      box-shadow: 0 6px 20px rgba(59, 130, 246, 0.4);
    }

    button.btn-primary:active {
      transform: translateY(1px);
    }

    button.btn-primary:disabled {
      background: rgba(255, 255, 255, 0.05);
      color: var(--text-muted);
      border: 1px solid var(--border);
      box-shadow: none;
      cursor: not-allowed;
    }

    button.btn-secondary {
      background: rgba(255, 255, 255, 0.03);
      color: var(--text);
      border: 1px solid var(--border);
    }

    button.btn-secondary:hover {
      background: rgba(255, 255, 255, 0.08);
      border-color: rgba(255, 255, 255, 0.2);
    }

    button.btn-icon {
      padding: 10px;
      border-radius: 10px;
    }

    /* Document Lists */
    .list-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-top: 10px;
      margin-bottom: 12px;
    }

    .list-title {
      font-family: var(--font-outfit);
      font-weight: 600;
      font-size: 14px;
      color: var(--text-muted);
    }

    .scroll-list {
      display: flex;
      flex-direction: column;
      gap: 10px;
      max-height: calc(100vh - 240px);
      overflow-y: auto;
      padding-right: 4px;
    }

    .doc-item {
      text-align: left;
      padding: 14px;
      border-radius: 14px;
      border: 1px solid var(--border);
      background: rgba(255, 255, 255, 0.02);
      cursor: pointer;
      color: var(--text);
      transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
      display: flex;
      flex-direction: column;
      gap: 6px;
    }

    .doc-item:hover, .doc-item.active {
      border-color: rgba(59, 130, 246, 0.4);
      background: rgba(59, 130, 246, 0.06);
      transform: translateX(2px);
    }

    .doc-item-title {
      font-family: var(--font-outfit);
      font-weight: 600;
      font-size: 14px;
      color: #fff;
    }

    .doc-item-meta {
      font-size: 11px;
      color: var(--text-muted);
      display: flex;
      align-items: center;
      gap: 6px;
    }

    .indicator-tag {
      display: flex;
      align-items: center;
      gap: 4px;
      font-size: 10px;
      font-weight: 600;
      padding: 2px 8px;
      border-radius: 10px;
      background: rgba(255, 255, 255, 0.03);
    }

    .indicator-tag.ready {
      color: var(--success);
      background: rgba(16, 185, 129, 0.08);
    }

    .indicator-tag.pending {
      color: var(--text-muted);
      background: rgba(255, 255, 255, 0.03);
    }

    .dot-pulse {
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: currentColor;
    }

    .ready .dot-pulse {
      box-shadow: 0 0 8px var(--success-glow);
    }

    /* Inputs */
    .input-wrapper {
      position: relative;
      display: flex;
      gap: 8px;
      width: 100%;
    }

    input[type="text"] {
      flex: 1;
      border-radius: 12px;
      border: 1px solid var(--border);
      background: rgba(255, 255, 255, 0.02);
      color: var(--text);
      padding: 12px 16px;
      font-family: var(--font-inter);
      font-size: 14px;
      transition: all 0.3s ease;
    }

    input[type="text"]:focus {
      outline: none;
      border-color: rgba(59, 130, 246, 0.5);
      background: rgba(255, 255, 255, 0.05);
      box-shadow: 0 0 15px rgba(59, 130, 246, 0.15);
    }

    /* Suggestion Chips */
    .chips-container {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 12px;
    }

    .chip {
      padding: 6px 12px;
      border-radius: 14px;
      background: rgba(255, 255, 255, 0.03);
      border: 1px solid var(--border);
      color: var(--text-muted);
      font-size: 12px;
      font-weight: 500;
      cursor: pointer;
      transition: all 0.3s ease;
    }

    .chip:hover {
      background: rgba(59, 130, 246, 0.08);
      border-color: rgba(59, 130, 246, 0.2);
      color: #60a5fa;
    }

    /* Search Results */
    .results-container {
      display: flex;
      flex-direction: column;
      gap: 12px;
      margin-top: 14px;
      max-height: calc(100vh - 340px);
      overflow-y: auto;
      padding-right: 4px;
    }

    .result-item {
      border: 1px solid var(--border);
      background: rgba(255, 255, 255, 0.01);
      border-radius: 16px;
      padding: 16px;
      transition: all 0.3s ease;
      display: flex;
      flex-direction: column;
      gap: 8px;
    }

    .result-item:hover {
      border-color: rgba(139, 92, 246, 0.3);
      background: rgba(139, 92, 246, 0.02);
    }

    .result-item-header {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 10px;
    }

    .result-item h4 {
      font-family: var(--font-outfit);
      font-weight: 600;
      font-size: 14px;
      color: #fff;
    }

    .result-score {
      font-family: var(--font-mono);
      font-size: 11px;
      color: #a78bfa;
      background: rgba(139, 92, 246, 0.1);
      padding: 2px 6px;
      border-radius: 6px;
      font-weight: 600;
    }

    .result-meta {
      font-size: 11px;
      color: var(--text-muted);
      display: flex;
      flex-wrap: wrap;
      gap: 8px 12px;
      border-bottom: 1px dashed var(--border);
      padding-bottom: 8px;
    }

    .result-text {
      font-size: 13px;
      color: #cbd5e1;
      line-height: 1.6;
    }

    /* Markdown Viewer Styles */
    .viewer-toolbar {
      display: flex;
      justify-content: space-between;
      align-items: center;
      border-bottom: 1px solid var(--border);
      padding-bottom: 14px;
      margin-bottom: 16px;
    }

    .tabs {
      display: flex;
      gap: 6px;
      background: rgba(255, 255, 255, 0.02);
      padding: 4px;
      border-radius: 12px;
      border: 1px solid var(--border);
    }

    .tab {
      padding: 8px 16px;
      border-radius: 8px;
      background: transparent;
      border: none;
      color: var(--text-muted);
      font-family: var(--font-outfit);
      font-weight: 600;
      font-size: 12px;
      cursor: pointer;
      transition: all 0.3s ease;
    }

    .tab.active {
      background: rgba(255, 255, 255, 0.06);
      color: #fff;
      box-shadow: 0 2px 8px rgba(0, 0, 0, 0.2);
    }

    .viewport {
      min-height: 380px;
      max-height: 520px;
      overflow-y: auto;
      border: 1px solid var(--border);
      background: rgba(3, 7, 18, 0.4);
      border-radius: 16px;
      padding: 24px;
      font-family: var(--font-inter);
      font-size: 14px;
      color: #cbd5e1;
      line-height: 1.7;
    }

    /* Markdown HTML Elements Styling */
    .viewport h1, .viewport h2, .viewport h3, .viewport h4 {
      font-family: var(--font-outfit);
      color: #fff;
      font-weight: 700;
      margin-top: 24px;
      margin-bottom: 12px;
    }
    .viewport h1 { font-size: 24px; border-bottom: 1px solid var(--border); padding-bottom: 6px; }
    .viewport h2 { font-size: 20px; }
    .viewport h3 { font-size: 17px; }
    .viewport p { margin-bottom: 14px; color: #cbd5e1; }
    .viewport ul, .viewport ol { margin-left: 20px; margin-bottom: 14px; }
    .viewport li { margin-bottom: 6px; }
    .viewport blockquote {
      border-left: 4px solid var(--primary);
      padding: 8px 16px;
      background: rgba(255, 255, 255, 0.02);
      margin-bottom: 14px;
      border-radius: 0 8px 8px 0;
      color: var(--text-muted);
    }
    .viewport code {
      font-family: var(--font-mono);
      font-size: 13px;
      background: rgba(255, 255, 255, 0.06);
      padding: 2px 6px;
      border-radius: 4px;
      color: #e2e8f0;
    }
    .viewport pre {
      background: #020617;
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 16px;
      margin-bottom: 14px;
      overflow-x: auto;
    }
    .viewport pre code {
      background: transparent;
      padding: 0;
      color: #cbd5e1;
    }
    .viewport table {
      width: 100%;
      border-collapse: collapse;
      margin-bottom: 16px;
    }
    .viewport th, .viewport td {
      border: 1px solid var(--border);
      padding: 10px 12px;
      text-align: left;
    }
    .viewport th {
      background: rgba(255, 255, 255, 0.04);
      color: #fff;
      font-weight: 600;
    }
    .viewport tr:nth-child(even) td {
      background: rgba(255, 255, 255, 0.01);
    }

    /* Glow Highlights */
    mark {
      background: linear-gradient(120deg, rgba(234, 179, 8, 0.2) 0%, rgba(234, 179, 8, 0.3) 100%);
      border-bottom: 2px solid #eab308;
      color: #fef08a;
      padding: 1px 3px;
      border-radius: 4px;
      font-weight: 500;
      text-shadow: 0 0 8px rgba(234, 179, 8, 0.4);
    }

    /* Status State Boxes */
    pre#resultBox {
      font-family: var(--font-mono);
      font-size: 12px;
      background: #020617;
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 16px;
      max-height: 200px;
      overflow: auto;
      margin: 0;
      white-space: pre-wrap;
      word-break: break-all;
    }

    /* Fragment Sidebar / Controls */
    .fragment-section {
      border-top: 1px solid var(--border);
      margin-top: 20px;
      padding-top: 20px;
    }

    .fragment-toolbar {
      display: flex;
      gap: 8px;
      margin-bottom: 12px;
    }

    .fragment-list {
      display: flex;
      flex-direction: column;
      gap: 8px;
      max-height: 250px;
      overflow-y: auto;
      padding-right: 4px;
      margin-bottom: 12px;
    }

    .fragment-item {
      text-align: left;
      padding: 10px 14px;
      border-radius: 12px;
      border: 1px solid var(--border);
      background: rgba(255, 255, 255, 0.01);
      cursor: pointer;
      color: var(--text);
      transition: all 0.3s ease;
      display: flex;
      flex-direction: column;
      gap: 4px;
    }

    .fragment-item:hover, .fragment-item.active {
      border-color: rgba(59, 130, 246, 0.3);
      background: rgba(59, 130, 246, 0.05);
    }

    .fragment-item-title {
      font-family: var(--font-outfit);
      font-weight: 600;
      font-size: 13px;
    }

    .fragment-meta-info {
      font-size: 11px;
      color: var(--text-muted);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .fragment-pagination {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
      font-size: 12px;
      color: var(--text-muted);
    }

    .pagination-btns {
      display: flex;
      gap: 6px;
    }

    .rotate-spinner {
      animation: rotating 1.5s linear infinite;
    }

    @keyframes rotating {
      from { transform: rotate(0deg); }
      to { transform: rotate(360deg); }
    }

    .loading-pulse {
      display: inline-block;
      width: 16px;
      height: 16px;
      border: 2px solid rgba(255,255,255,0.1);
      border-top-color: var(--primary);
      border-radius: 50%;
      animation: rotating 1s linear infinite;
    }
  </style>
</head>
<body>
  <noscript>
    <div style="position: fixed; top: 0; left: 0; right: 0; background: #ef4444; color: white; padding: 16px; font-family: sans-serif; font-size: 14px; z-index: 999999; text-align: center; font-weight: bold; box-shadow: 0 4px 15px rgba(0,0,0,0.5);">
      &#9888; JavaScript esta desactivado o bloqueado en tu navegador. Para utilizar Busca Ordenanza, activa JavaScript en la configuracion del navegador o desactiva extensiones de bloqueo para este sitio.
    </div>
  </noscript>

  <!-- Floating Diagnostic Panel -->
  <div id="jsDiagnostics" style="position: fixed; bottom: 24px; right: 24px; background: rgba(17, 24, 39, 0.95); color: #10b981; border: 1px solid #10b981; padding: 16px; border-radius: 16px; font-family: 'JetBrains Mono', monospace; font-size: 11px; z-index: 99999; text-align: left; max-width: 320px; box-shadow: 0 10px 30px rgba(0,0,0,0.5); backdrop-filter: blur(8px); display: flex; flex-direction: column; gap: 6px;">
    <strong style="color: #fff; font-family: 'Outfit', sans-serif; font-size: 13px; border-bottom: 1px solid rgba(255,255,255,0.1); padding-bottom: 4px; margin-bottom: 4px; display: flex; align-items: center; gap: 6px;">
      <span style="width: 8px; height: 8px; border-radius: 50%; background: #10b981; box-shadow: 0 0 8px rgba(16, 185, 129, 0.5);"></span>
      Diagnóstico de Frontend
    </strong>
    <div>â¢ Script cargado: <span id="diagScript" style="color: #ef4444; font-weight: bold;">NO</span></div>
    <div>â¢ DOM listo: <span id="diagDOM" style="color: #ef4444; font-weight: bold;">NO</span></div>
    <div>â¢ init() completado: <span id="diagInit" style="color: #ef4444; font-weight: bold;">NO</span></div>
    <div>â¢ loadDocuments(): <span id="diagLoadDocs" style="color: #ef4444; font-weight: bold;">NO</span></div>
    <div id="diagErrorBox" style="display: none; margin-top: 8px; padding: 8px; background: rgba(239, 68, 68, 0.1); border: 1px solid rgba(239, 68, 68, 0.2); border-radius: 8px; color: #f87171; max-height: 120px; overflow-y: auto; white-space: pre-wrap; font-size: 10px;"></div>
  </div>

  <header>
    <div class="logo-container">
      <h1>Busca Ordenanza</h1>
      <p>NotebookLM Inteligente para Leyes y Resoluciones</p>
    </div>
    
    <div id="meiliStatus" class="status-badge">
      <span class="status-dot"></span>
      <span id="meiliStatusText">Comprobando estado...</span>
    </div>
  </header>

  <main>
    <!-- Left Sidebar: Uploader & Document List -->
    <div class="col col-sidebar">


      <!-- Document list -->
      <section class="card" style="flex: 1;">
        <div class="card-header-area" style="margin-bottom: 16px;">
          <h2 class="card-title">Biblioteca</h2>
          <button id="refreshDocs" class="btn-secondary btn-icon" title="Actualizar lista" style="background: rgba(255, 255, 255, 0.02); border: 1px solid var(--border); padding: 8px; border-radius: 50%; width: 34px; height: 34px; display: flex; align-items: center; justify-content: center; transition: all 0.3s ease; box-shadow: 0 4px 10px rgba(0,0,0,0.15);">
            <svg width="16" height="16" fill="none" viewBox="0 0 24 24" stroke="currentColor" style="display: block;">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 1121.21 8H18.5" />
            </svg>
          </button>
        </div>
        
        <div class="scroll-list" id="docList">
          <p class="description-text" style="text-align: center; font-style: italic;">Cargando biblioteca...</p>
        </div>
      </section>
    </div>

    <!-- Center Column: Search and Search results -->
    <div class="col col-sidebar">
      <!-- Search card -->
      <section class="card">
        <span class="pill-badge" style="width: fit-content; background: rgba(139, 92, 246, 0.1); border-color: rgba(139, 92, 246, 0.15); color: #c084fc;">Búsqueda Semántica</span>
        <h2 class="card-title" style="margin-top: 8px;">Consultar</h2>
        <p class="description-text">Busca de forma instantánea en todas las ordenanzas de la biblioteca.</p>
        
        <form id="searchForm" class="input-wrapper">
          <input id="query" name="query" type="text" placeholder="Palabras clave a buscar..." required>
          <button type="submit" class="btn-primary">
            <svg width="16" height="16" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
            </svg>
            <span>Buscar</span>
          </button>
        </form>

        <div class="chips-container" id="suggestionChips">
          <span class="chip">ordenanza</span>
          <span class="chip">tasa municipal</span>
          <span class="chip">presupuesto</span>
          <span class="chip">licitación pública</span>
          <span class="chip">multa</span>
        </div>
      </section>

      <!-- Results Card -->
      <section class="card" style="flex: 1;">
        <h2 class="card-title">Resultados globales</h2>
        <p class="description-text" style="margin-bottom: 10px;">Fragmentos más relevantes encontrados en los documentos.</p>
        
        <div class="results-container" id="searchResults">
          <p class="description-text" style="text-align: center; font-style: italic;">Escribe algo y presiona Buscar para consultar los índices.</p>
        </div>

      </section>
    </div>

    <!-- Right Workspace: Document viewer and fragments navigation -->
    <div class="col col-viewer">
      <section class="card" style="flex: 1;">
        <div class="viewer-toolbar">
          <div class="logo-container">
            <h2 class="card-title" id="viewerTitle">Visor de Documentos</h2>
            <p id="viewerMetaText" class="description-text" style="margin-bottom: 0;">Selecciona un documento para comenzar a leer.</p>
          </div>
          
          <div style="display: flex; gap: 8px;">
            <button id="downloadOriginalPDF" class="btn-secondary" disabled>
              <span>Descargar PDF</span>
              <svg width="14" height="14" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
              </svg>
            </button>
            <button id="openRawMarkdown" class="btn-secondary" disabled>
              <span>Crudo</span>
              <svg width="14" height="14" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
              </svg>
            </button>
          </div>
        </div>

        <!-- View Mode Tabs -->
        <div class="viewer-toolbar" style="border: none; padding: 0; margin-bottom: 14px;">
          <div class="tabs">
            <button id="tabComplete" class="tab active">Lectura Completa</button>
            <button id="tabFragment" class="tab" disabled>Fragmento Seleccionado</button>
            <button id="tabPDF" class="tab" disabled>PDF Original</button>
          </div>
          <div id="viewerLoading" class="loading-pulse" style="display: none;"></div>
        </div>

        <!-- Content Area -->
        <div class="viewport" id="markdownViewer">
          <div style="height: 100%; display: flex; flex-direction: column; justify-content: center; align-items: center; text-align: center; padding: 40px 0;">
            <svg width="48" height="48" fill="none" viewBox="0 0 24 24" stroke="currentColor" style="color: var(--text-muted); opacity: 0.5; margin-bottom: 16px;">
              <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M12 6.253v13m0-13C10.832 5.477 9.246 5 7.5 5S4.168 5.477 3 6.253v13C4.168 18.477 5.754 18 7.5 18s3.332.477 4.5 1.253m0-13C13.168 5.477 14.754 5 16.5 5c1.747 0 3.332.477 4.5 1.253v13C19.832 18.477 18.247 18 16.5 18c-1.746 0-3.332.477-4.5 1.253" />
            </svg>
            <h3 style="font-family: var(--font-outfit); font-weight: 600; color: #fff;">Biblioteca Vacíoa</h3>
            <p class="description-text" style="max-width: 250px; margin-top: 6px;">Selecciona o carga una ordenanza en el panel izquierdo para desplegar su contenido aquí.</p>
          </div>
        </div>

        <!-- Fragments Area -->
        <div class="fragment-section">
          <h3 class="list-title" style="margin-bottom: 12px; display: flex; align-items: center; gap: 8px;">
            <span>Fragmentos del Documento</span>
            <span class="pill-badge" style="font-size: 9px; padding: 2px 6px;" id="fragCountBadge">0</span>
          </h3>
          
          <div class="fragment-toolbar">
            <input id="fragmentQuery" type="text" placeholder="Filtrar fragmentos locales..." style="padding: 8px 12px;">
            <button id="fragmentSearch" class="btn-secondary" style="padding: 8px 14px;">Filtrar</button>
            <button id="fragmentClear" class="btn-secondary" style="padding: 8px 14px;">Limpiar</button>
          </div>

          <div class="fragment-list" id="fragmentList">
            <p class="description-text" style="font-style: italic; text-align: center; margin-top: 10px;">Ningún documento activo.</p>
          </div>

          <div class="fragment-pagination">
            <div id="fragmentPageInfo">Selecciona un documento para cargar fragmentos.</div>
            <div class="pagination-btns">
              <button id="fragmentPrev" class="btn-secondary btn-icon" style="padding: 6px 12px;" disabled>â</button>
              <button id="fragmentNext" class="btn-secondary btn-icon" style="padding: 6px 12px;" disabled>âº</button>
            </div>
          </div>
        </div>
      </section>
    </div>
  </main>

  <script src="/api/app.js"></script>
</body>
</html>
        ''',
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    )


@app.get('/api/marked.min.js', response_class=Response)
def get_marked_js() -> Response:
    marked_path = BASE_DIR / 'app' / 'marked.min.js'
    if not marked_path.exists():
        raise HTTPException(status_code=404, detail='File not found')
    return Response(content=marked_path.read_bytes(), media_type='application/javascript')



@app.get('/api/app.js', response_class=Response)
def get_app_js() -> Response:
    app_js_path = BASE_DIR / 'app' / 'app.js'
    if not app_js_path.exists():
        raise HTTPException(status_code=404, detail='File not found')
    return Response(content=app_js_path.read_bytes(), media_type='application/javascript')


@app.get('/api/documentos')
def api_documents() -> list[dict]:
    return list_markdown_documents()


@app.get('/api/pdfs')
def api_pdfs() -> list[dict]:
  return list_uploaded_documents()


@app.get('/api/documentos/{document_id}/markdown')
def api_document_markdown(document_id: str) -> dict:
    try:
        markdown = read_markdown_document(document_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail='Documento no encontrado')
    return {'document_id': document_id, 'markdown': markdown}


@app.get('/api/documentos/{document_id}/fragmentos')
def api_document_fragments(
    document_id: str,
    q: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=5, ge=1, le=20),
) -> dict:
    try:
        fragments = build_fragments(document_id, q)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail='Documento no encontrado')

    return paginate_fragments(fragments, page, page_size)


@app.get('/api/documentos/{document_id}/markdown-raw', response_class=PlainTextResponse)
def api_document_markdown_raw(document_id: str) -> str:
    try:
        return read_markdown_document(document_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail='Documento no encontrado')


@app.get('/api/documentos/{document_id}/pdf')
def api_document_pdf(document_id: str, download: bool = Query(default=False)):
    from fastapi.responses import FileResponse
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    for uploaded_file in UPLOAD_DIR.iterdir():
        if uploaded_file.is_file() and uploaded_file.stem == document_id:
            if uploaded_file.suffix.lower() == '.pdf':
                if download:
                    return FileResponse(
                        path=uploaded_file, 
                        media_type='application/pdf', 
                        filename=uploaded_file.name
                    )
                else:
                    return FileResponse(
                        path=uploaded_file, 
                        media_type='application/pdf', 
                        headers={
                            "Content-Disposition": "inline",
                            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                            "Pragma": "no-cache",
                            "Expires": "0"
                        }
                    )
            else:
                return HTMLResponse(
                    content='''
                    <!DOCTYPE html>
                    <html lang="es">
                    <head>
                        <meta charset="utf-8">
                        <style>
                            body {
                                margin: 0;
                                padding: 0;
                                background: #030712;
                                color: #9ca3af;
                                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
                                display: flex;
                                flex-direction: column;
                                justify-content: center;
                                align-items: center;
                                height: 100vh;
                                text-align: center;
                            }
                            .card {
                                background: rgba(17, 24, 39, 0.6);
                                border: 1px solid rgba(255, 255, 255, 0.08);
                                border-radius: 16px;
                                padding: 32px 24px;
                                max-width: 360px;
                                box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5);
                            }
                            h3 {
                                color: #fff;
                                font-size: 18px;
                                margin-top: 0;
                                margin-bottom: 8px;
                            }
                            p {
                                font-size: 13px;
                                line-height: 1.6;
                                margin-bottom: 16px;
                            }
                            .hint {
                                font-size: 11px;
                                color: #6b7280;
                            }
                            svg {
                                color: #f59e0b;
                                margin-bottom: 16px;
                            }
                        </style>
                    </head>
                    <body>
                        <div class="card">
                            <svg width="48" height="48" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
                            </svg>
                            <h3>Documento Original Word</h3>
                            <p>Este documento fue importado desde un archivo de Microsoft Word (.doc/.docx). Por lo tanto, no posee un archivo PDF original para previsualizar directamente.</p>
                            <div class="hint">Utiliza las pestañas "Lectura Completa" o "Fragmento Seleccionado" para ver el texto.</div>
                        </div>
                    </body>
                    </html>
                    ''',
                    status_code=200
                )
    raise HTTPException(status_code=404, detail='PDF no encontrado')


@app.post('/api/subida-archivos', response_model=None)
async def upload_files(request: Request, files: list[UploadFile] = File(...), index_to_meili: bool = Form(True)):
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    MARKDOWN_DIR.mkdir(parents=True, exist_ok=True)
    JSON_DIR.mkdir(parents=True, exist_ok=True)

    processed = []

    for file in files:
        suffix = Path(file.filename).suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            raise HTTPException(status_code=400, detail=f'Formato no soportado: {file.filename}')

        # Sanitize original filename to keep it clean and safe for Meilisearch IDs and file paths
        original_name = Path(file.filename).name
        sanitized_name = re.sub(r'[^a-zA-Z0-9-._]', '_', original_name)
        temp_name = f'{uuid4().hex}_{sanitized_name}'
        temp_path = UPLOAD_DIR / temp_name

        with temp_path.open('wb') as buffer:
            shutil.copyfileobj(file.file, buffer)

        result = process_uploaded_file(
            input_path=str(temp_path),
            markdown_dir=str(MARKDOWN_DIR),
            json_dir=str(JSON_DIR),
            index_to_meili=index_to_meili,
        )
        processed.append(result)

    result = {
        'message': 'Archivos procesados correctamente',
        'files': processed,
    }

    if request.headers.get('x-requested-with') == 'fetch':
      return result

    uploaded_names = ','.join(item['source_file'] for item in processed if item.get('source_file'))
    return RedirectResponse(url=f"/?upload=success&files={uploaded_names}", status_code=303)


@app.post('/api/log-error')
async def log_error(request: Request):
    try:
        import json
        import time
        data = await request.json()
        print(f"\n[!!! BROWSER ERROR DETECTED !!!]")
        print(f"Message: {data.get('message')}")
        print(f"Source: {data.get('source')}")
        print(f"Line: {data.get('lineno')}:{data.get('colno')}")
        print(f"Stack: {data.get('stack')}\n")
        
        log_path = BASE_DIR / 'data' / 'browser_errors.json'
        log_path.parent.mkdir(parents=True, exist_ok=True)
        
        logs = []
        if log_path.exists():
            try:
                logs = json.loads(log_path.read_text(encoding='utf-8'))
            except Exception:
                pass
        
        logs.append({
            'timestamp': time.time(),
            'message': data.get('message'),
            'source': data.get('source'),
            'lineno': data.get('lineno'),
            'colno': data.get('colno'),
            'stack': data.get('stack')
        })
        log_path.write_text(json.dumps(logs, indent=2), encoding='utf-8')
    except Exception as e:
        print(f"Error parsing log-error payload: {e}")
    return {"status": "logged"}


@app.post('/api/buscar')
def search_documents(payload: SearchRequest) -> dict:
    try:
        service = MeiliService.from_settings()
        results = service.search(payload.query, payload.limit)
        return {
            'query': payload.query,
            'limit': payload.limit,
            'results': results,
            'message': 'Resultados obtenidos desde Meilisearch',
        }
    except Exception as exc:
        local_hits = []
        try:
            documents = list_markdown_documents()
            all_fragments = []
            for doc in documents:
                doc_id = doc['id']
                fragments = build_fragments(doc_id, payload.query)
                for frag in fragments:
                    all_fragments.append({
                        'id': frag['id'],
                        'document_id': doc_id,
                        'title': doc['title'],
                        'section': frag['section'],
                        'source': f"{doc_id}.md",
                        'content_text': frag['content_text'],
                        'score': frag['score']
                    })
            all_fragments.sort(key=lambda x: (-x['score'], x['id']))
            local_hits = all_fragments[:payload.limit]
            message = f"Resultados locales fallback (Meilisearch desconectado): {exc}"
        except Exception as local_err:
            message = f"No se pudo consultar Meilisearch: {exc}. Fallback local falló: {local_err}"
            
        return {
            'query': payload.query,
            'limit': payload.limit,
            'results': {'hits': local_hits},
            'message': message,
        }
