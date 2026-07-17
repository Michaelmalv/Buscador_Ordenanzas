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
from ingestion.workflow import process_uploaded_file, get_s3_client

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
    s3_client = get_s3_client()
    documents: list[dict] = []
    if s3_client:
        try:
            response = s3_client.list_objects_v2(Bucket=settings.bucket_name, Prefix="markdown/")
            for obj in response.get('Contents', []):
                key = obj['Key']
                if key.endswith('.md'):
                    stem = Path(key).stem
                    documents.append(
                        {
                            'id': stem,
                            'title': humanize_stem(stem),
                            'filename': Path(key).name,
                        }
                    )
        except Exception as e:
            print(f"Error listando markdown en R2: {e}")
    else:
        MARKDOWN_DIR.mkdir(parents=True, exist_ok=True)
        for markdown_file in sorted(MARKDOWN_DIR.glob('*.md')):
            documents.append(
                {
                    'id': markdown_file.stem,
                    'title': humanize_stem(markdown_file.stem),
                    'filename': markdown_file.name,
                }
            )

    return sorted(documents, key=lambda x: x['title'])


def get_document_category(doc_id: str) -> str:
    doc_id_upper = doc_id.upper()
    if doc_id_upper.startswith("ORD_"):
        return "Ordenanzas"
    elif doc_id_upper.startswith("RES_RC"):
        return "Resoluciones de Concejo"
    elif doc_id_upper.startswith("RES_RA") or doc_id_upper.startswith("RES_RADMQ") or doc_id_upper.startswith("RES_RAQ"):
        return "Resoluciones de Alcaldía"
    elif "CONCEJO" in doc_id_upper or "RC" in doc_id_upper:
        return "Resoluciones de Concejo"
    else:
        return "Resoluciones de Alcaldía"


def list_uploaded_documents() -> list[dict]:
    s3_client = get_s3_client()
    documents: list[dict] = []
    if s3_client:
        try:
            # 1. Obtener los stems de los markdown convertidos usando un paginador
            converted_stems = set()
            paginator = s3_client.get_paginator('list_objects_v2')
            
            md_pages = paginator.paginate(Bucket=settings.bucket_name, Prefix="markdown/")
            for page in md_pages:
                if 'Contents' in page:
                    for obj in page['Contents']:
                        key = obj['Key']
                        if key.endswith('.md'):
                            converted_stems.add(Path(key).stem)

            # 2. Listar los archivos originales usando un paginador
            up_pages = paginator.paginate(Bucket=settings.bucket_name, Prefix="uploads/")
            for page in up_pages:
                if 'Contents' in page:
                    for obj in page['Contents']:
                        key = obj['Key']
                        filename = Path(key).name
                        stem = Path(key).stem
                        
                        # Evitar agregar la carpeta base
                        if not filename:
                            continue
                            
                        parts = filename.split('_', 1)
                        if len(parts) > 1 and len(parts[0]) == 32 and all(c in '0123456789abcdefABCDEF' for c in parts[0]):
                            original_filename = parts[1]
                        else:
                            original_filename = filename

                        documents.append(
                            {
                                'id': stem,
                                'title': display_name_from_upload(filename),
                                'filename': filename,
                                'original_filename': original_filename,
                                'converted': stem in converted_stems,
                                'category': get_document_category(stem),
                            }
                        )
        except Exception as e:
            print(f"Error listando uploads en R2: {e}")
        return sorted(documents, key=lambda x: x['title'])
    else:
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        
        # Process new manual uploads before listing
        sync_manual_uploads()
        
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
                    'category': get_document_category(uploaded_file.stem),
                }
            )

        return documents


def read_markdown_document(document_id: str) -> str:
    s3_client = get_s3_client()
    if s3_client:
        try:
            response = s3_client.get_object(Bucket=settings.bucket_name, Key=f"markdown/{document_id}.md")
            return response['Body'].read().decode('utf-8')
        except s3_client.exceptions.NoSuchKey:
            raise FileNotFoundError(document_id)
        except Exception as e:
            print(f"Error descargando markdown de R2 para {document_id}: {e}")
            raise FileNotFoundError(document_id)
    else:
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
    matched_terms = 0
    
    for term in parsed_query:
        count = haystack.count(term['text'])
        if count > 0:
            if not term['is_phrase']:
                matched_terms += 1
                # Cap the word frequency contribution to avoid keyword-stuffing skew
                score += min(count, 5) * term['weight']
            else:
                # Massive boost for exact phrase match
                score += count * 50000
                
    # Boost for matching more unique terms
    score += matched_terms * 10000
    return score


def score_fragment(fragment: dict, fragment_query: str | None = None) -> int:
    normalized_query = fragment_query.strip().lower() if fragment_query else ''
    if not normalized_query:
        return 0

    haystack = f"{fragment['section']} {fragment['content_text']}".lower()
    parsed_query = parse_search_query(normalized_query)
    return score_fragment_parsed(haystack, parsed_query)


_fragments_cache: dict[str, list[dict]] = {}

def get_cached_fragments(document_id: str) -> list[dict]:
    if document_id in _fragments_cache:
        return _fragments_cache[document_id]

    s3_client = get_s3_client()
    if s3_client:
        try:
            import json
            response = s3_client.get_object(Bucket=settings.bucket_name, Key=f"json/{document_id}.json")
            data = json.loads(response['Body'].read().decode('utf-8'))
            fragments = []
            for chunk in data:
                c_markdown = chunk.get('content_markdown', '').strip()
                if not c_markdown:
                    continue
                fragments.append({
                    'id': f"{document_id}-fragment-{chunk['chunk_number']}",
                    'document_id': document_id,
                    'chunk_number': chunk['chunk_number'],
                    'section': chunk.get('section', ''),
                    'content_markdown': c_markdown,
                    'content_text': chunk.get('content_text', ''),
                    'char_count': len(c_markdown),
                })
            _fragments_cache[document_id] = fragments
            return fragments
        except Exception as e:
            print(f"Error descargando o parseando JSON cache de R2 para {document_id}: {e}")
    else:
        # Try loading from the pre-computed JSON file locally
        json_path = JSON_DIR / f'{document_id}.json'
        if json_path.exists():
            try:
                import json
                with open(json_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                fragments = []
                for chunk in data:
                    c_markdown = chunk.get('content_markdown', '').strip()
                    if not c_markdown:
                        continue
                    fragments.append({
                        'id': f"{document_id}-fragment-{chunk['chunk_number']}",
                        'document_id': document_id,
                        'chunk_number': chunk['chunk_number'],
                        'section': chunk.get('section', ''),
                        'content_markdown': c_markdown,
                        'content_text': chunk.get('content_text', ''),
                        'char_count': len(c_markdown),
                    })
                _fragments_cache[document_id] = fragments
                return fragments
            except Exception as e:
                print(f"Error loading local JSON cache for {document_id}: {e}")

    # Fallback: parse Markdown (works both locally and with R2 via updated read_markdown_document)
    fragments = build_fragments_from_markdown(document_id)
    _fragments_cache[document_id] = fragments
    return fragments


def build_fragments_from_markdown(document_id: str) -> list[dict]:
    markdown = read_markdown_document(document_id)
    fragments: list[dict] = []
    for index, chunk in enumerate(split_markdown_by_headers(markdown), start=1):
        content_markdown = chunk['content_markdown'].strip()
        if not content_markdown:
            continue
        content_text = markdown_to_plain_text(content_markdown)
        fragments.append({
            'id': f'{document_id}-fragment-{index}',
            'document_id': document_id,
            'chunk_number': index,
            'section': chunk['section'],
            'content_markdown': content_markdown,
            'content_text': content_text,
            'char_count': len(content_markdown),
        })
    return fragments


def build_fragments(document_id: str, fragment_query: str | None = None) -> list[dict]:
    try:
        all_fragments = get_cached_fragments(document_id)
    except FileNotFoundError:
        raise FileNotFoundError(document_id)

    normalized_query = fragment_query.strip().lower() if fragment_query else ''
    parsed_query = parse_search_query(normalized_query) if normalized_query else []

    filtered_fragments = []
    for frag in all_fragments:
        haystack = f"{frag['section']} {frag['content_text']}".lower()

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

        # Copy fragment to score it without modifying the cached version
        frag_copy = dict(frag)
        frag_copy['score'] = score_fragment_parsed(haystack, parsed_query)
        filtered_fragments.append(frag_copy)

    if normalized_query:
        filtered_fragments.sort(key=lambda item: (-item['score'], item['chunk_number']))

    return filtered_fragments


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


import threading
import time

_meili_online = False

_in_memory_index = None
_in_memory_index_lock = threading.Lock()

def get_in_memory_index():
    global _in_memory_index
    if _in_memory_index is not None:
        return _in_memory_index
    
    with _in_memory_index_lock:
        if _in_memory_index is not None:
            return _in_memory_index
        
        start_time = time.time()
        loaded = False
        
        # Try local gzip index first
        local_path = BASE_DIR / "search_index.json.gz"
        if local_path.exists():
            print("[*] Cargando indice de busqueda comprimido desde archivo local...")
            try:
                import gzip
                import json
                with gzip.open(local_path, "rt", encoding="utf-8") as f:
                    _in_memory_index = json.load(f)
                loaded = True
            except Exception as e:
                print(f"Error cargando indice local: {e}")

        # Try uncompressed local path as fallback
        if not loaded:
            local_raw_path = BASE_DIR / "search_index.json"
            if local_raw_path.exists():
                print("[*] Cargando indice de busqueda sin comprimir desde archivo local...")
                try:
                    import json
                    with open(local_raw_path, "r", encoding="utf-8") as f:
                        _in_memory_index = json.load(f)
                    loaded = True
                except Exception as e:
                    print(f"Error cargando indice local sin comprimir: {e}")
        
        # Download compressed from R2
        if not loaded:
            print("[*] Descargando indice de busqueda comprimido desde Cloudflare R2...")
            try:
                s3 = get_s3_client()
                if s3:
                    response = s3.get_object(Bucket=settings.bucket_name, Key="search_index.json.gz")
                    gzip_body = response['Body'].read()
                    import gzip
                    import json
                    decompressed = gzip.decompress(gzip_body).decode('utf-8')
                    _in_memory_index = json.loads(decompressed)
                    loaded = True
                else:
                    print("[ERROR] No se pudo obtener el cliente de S3 para descargar el indice.")
            except Exception as e:
                print(f"[ERROR] Error descargando el indice de R2 (GZIP): {e}")

        # Fallback to uncompressed R2 download
        if not loaded:
            print("[*] Descargando indice de busqueda sin comprimir desde Cloudflare R2...")
            try:
                s3 = get_s3_client()
                if s3:
                    response = s3.get_object(Bucket=settings.bucket_name, Key="search_index.json")
                    content = response['Body'].read().decode('utf-8')
                    import json
                    _in_memory_index = json.loads(content)
                    loaded = True
            except Exception as e:
                print(f"[ERROR] Error descargando el indice sin comprimir de R2: {e}")
        
        if not loaded or _in_memory_index is None:
            _in_memory_index = []
            return _in_memory_index
            
        # Precalculate lowercase search haystacks for high performance
        print("[*] Precalculando indices de busqueda en memoria para velocidad instantanea...")
        for chunk in _in_memory_index:
            chunk['_haystack'] = f"{chunk.get('title', '')} {chunk.get('section', '')} {chunk.get('content_text', '')}".lower()
            
        print(f"[*] Indice completamente cargado y optimizado en {time.time() - start_time:.2f}s ({len(_in_memory_index)} fragmentos).")
        return _in_memory_index

def check_meili_loop():
    global _meili_online
    while True:
        try:
            service = MeiliService.from_settings()
            # Use short timeout of 0.5s for checking health
            health_status = service.client.health()
            _meili_online = (health_status.get('status') in ('available', 'ok'))
        except Exception:
            _meili_online = False
        time.sleep(10)

def warm_cache():
    try:
        documents = list_markdown_documents()
        if len(documents) > 100:
            print(f"[!] Gran cantidad de documentos detectados ({len(documents)}). Precalentando caché en segundo plano de manera diferida...")
        
        for doc in documents:
            doc_id = doc['id']
            get_cached_fragments(doc_id)
        print(f"[*] Cache precalentado: {len(documents)} documentos cargados en memoria.")
    except Exception as e:
        print(f"Error calentando cache: {e}")

@app.on_event("startup")
def startup_event():
    import os
    is_vercel = os.environ.get("VERCEL") == "1" or "VERCEL_ENV" in os.environ
    if is_vercel:
        print("[*] Ejecutándose en Vercel (modo serverless). Omitiendo precalentamiento para evitar timeout de cold-start.")
        threading.Thread(target=check_meili_loop, daemon=True).start()
    else:
        threading.Thread(target=warm_cache, daemon=True).start()
        threading.Thread(target=check_meili_loop, daemon=True).start()


@app.get('/health')
def health() -> dict[str, str]:
    return {
        'status': 'ok',
        'meilisearch': 'connected' if _meili_online else 'disconnected'
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
    content='''<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Busca Ordenanza — Búsqueda Inteligente</title>
  
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
      --primary: #1e40af;       /* Deep Azul Royal */
      --primary-hover: #1d4ed8;
      --accent: #dc2626;        /* Rojo Carmesí */
      --accent-hover: #b91c1c;
      --success: #16a34a;
      --success-glow: rgba(22, 163, 74, 0.15);
      --warning: #ea580c;
      --bg: #f8fafc;            /* Gris Muy Claro */
      --sidebar-bg: #f1f5f9;    /* Gris Claro para el menú lateral */
      --sidebar-hover: #e2e8f0;
      --card-bg: #ffffff;       /* Blanco */
      --border: #cbd5e1;        /* Gris Borde */
      --text: #0f172a;          /* Slate 900 */
      --text-muted: #475569;    /* Slate 600 */
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
      background: var(--bg);
      color: var(--text);
      font-family: var(--font-inter);
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      overflow-x: hidden;
      line-height: 1.5;
    }

    /* Scrollbars */
    ::-webkit-scrollbar {
      width: 6px;
      height: 6px;
    }
    ::-webkit-scrollbar-track {
      background: #f1f5f9;
    }
    ::-webkit-scrollbar-thumb {
      background: #cbd5e1;
      border-radius: 3px;
    }
    ::-webkit-scrollbar-thumb:hover {
      background: #94a3b8;
    }

    /* Layout structure */
    .app-container {
      display: flex;
      min-height: 100vh;
      width: 100%;
    }

    /* Sidebar Menu */
    .sidebar-menu {
      width: 280px;
      background: var(--sidebar-bg);
      border-right: 1px solid var(--border);
      padding: 24px 16px;
      display: flex;
      flex-direction: column;
      gap: 24px;
      flex-shrink: 0;
    }

    .sidebar-brand {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 0 8px;
    }

    .sidebar-brand h1 {
      font-family: var(--font-outfit);
      font-weight: 800;
      font-size: 20px;
      color: var(--primary);
      letter-spacing: -0.5px;
      display: flex;
      align-items: center;
      gap: 8px;
    }

    .sidebar-brand h1::before {
      content: '';
      display: inline-block;
      width: 10px;
      height: 10px;
      background: var(--accent);
      border-radius: 50%;
    }

    .menu-section {
      display: flex;
      flex-direction: column;
      gap: 6px;
    }

    .menu-section-title {
      font-family: var(--font-outfit);
      font-size: 11px;
      font-weight: 700;
      color: var(--text-muted);
      letter-spacing: 1px;
      text-transform: uppercase;
      padding: 0 8px;
      margin-bottom: 4px;
    }

    .menu-item {
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 12px 14px;
      background: transparent;
      border: none;
      border-radius: 12px;
      color: var(--text-muted);
      font-family: var(--font-outfit);
      font-weight: 600;
      font-size: 14px;
      cursor: pointer;
      text-align: left;
      transition: all 0.2s ease;
      width: 100%;
      text-decoration: none;
    }

    .menu-item:hover {
      background: var(--sidebar-hover);
      color: var(--text);
    }

    .menu-item.active {
      background: #ffffff;
      color: var(--primary);
      box-shadow: 0 4px 12px rgba(15, 23, 42, 0.05);
      border-left: 4px solid var(--accent);
      border-radius: 0 12px 12px 0;
      padding-left: 10px; /* Offset the 4px border */
    }

    .menu-badge {
      margin-left: auto;
      background: var(--primary);
      color: #ffffff;
      font-size: 11px;
      font-family: var(--font-mono);
      font-weight: 600;
      padding: 2px 8px;
      border-radius: 10px;
    }

    .menu-item.active .menu-badge {
      background: var(--accent);
    }

    /* Main Content Layout */
    main {
      flex: 1;
      display: flex;
      align-items: stretch;
      min-width: 0;
    }

    /* Column Container (Search or Category List) */
    .col-content {
      width: 420px;
      background: var(--bg);
      border-right: 1px solid var(--border);
      display: flex;
      flex-direction: column;
      flex-shrink: 0;
    }

    /* Column Viewer Container */
    .col-viewer {
      flex: 1;
      background: #ffffff;
      display: flex;
      flex-direction: column;
      min-width: 0;
    }

    /* Header for Panels */
    .panel-header {
      padding: 20px 24px;
      border-bottom: 1px solid var(--border);
      display: flex;
      flex-direction: column;
      gap: 6px;
      background: #ffffff;
    }

    .panel-header h2 {
      font-family: var(--font-outfit);
      font-weight: 700;
      font-size: 18px;
      color: var(--text);
      display: flex;
      align-items: center;
      gap: 8px;
    }

    .panel-header p {
      color: var(--text-muted);
      font-size: 13px;
    }

    /* Cards */
    .card {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 20px;
      box-shadow: 0 4px 6px -1px rgba(0,0,0,0.05), 0 2px 4px -1px rgba(0,0,0,0.02);
      display: flex;
      flex-direction: column;
      margin: 16px 24px 0 24px;
    }

    .card:last-child {
      margin-bottom: 24px;
    }

    .card-title {
      font-family: var(--font-outfit);
      font-weight: 700;
      font-size: 16px;
      color: var(--text);
      margin-bottom: 10px;
      display: flex;
      align-items: center;
      gap: 8px;
    }

    .pill-badge {
      display: inline-block;
      padding: 4px 8px;
      border-radius: 12px;
      background: rgba(30, 64, 175, 0.08);
      border: 1px solid rgba(30, 64, 175, 0.15);
      color: var(--primary);
      font-size: 11px;
      font-weight: 600;
      font-family: var(--font-outfit);
      text-transform: uppercase;
      letter-spacing: 0.5px;
      margin-bottom: 12px;
      width: fit-content;
    }

    .description-text {
      color: var(--text-muted);
      font-size: 13px;
      margin-bottom: 14px;
    }

    /* Buttons */
    button, .btn {
      padding: 10px 18px;
      border-radius: 10px;
      border: none;
      font-family: var(--font-outfit);
      font-weight: 700;
      font-size: 13px;
      cursor: pointer;
      transition: all 0.2s ease;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
    }

    button.btn-primary {
      background: var(--primary);
      color: #ffffff;
    }

    button.btn-primary:hover:not(:disabled) {
      background: var(--primary-hover);
    }

    button.btn-primary:disabled {
      background: #cbd5e1;
      color: #94a3b8;
      cursor: not-allowed;
    }

    button.btn-secondary {
      background: #ffffff;
      color: var(--text);
      border: 1px solid var(--border);
    }

    button.btn-secondary:hover {
      background: #f1f5f9;
      border-color: #cbd5e1;
    }

    button.btn-icon {
      padding: 8px;
      border-radius: 8px;
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
      border-radius: 10px;
      border: 1px solid var(--border);
      background: #ffffff;
      color: var(--text);
      padding: 10px 14px;
      font-family: var(--font-inter);
      font-size: 13px;
      transition: all 0.2s ease;
    }

    input[type="text"]:focus {
      outline: none;
      border-color: var(--primary);
      box-shadow: 0 0 0 3px rgba(30, 64, 175, 0.15);
    }

    /* Chips */
    .chips-container {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 10px;
    }

    .chip {
      padding: 5px 10px;
      border-radius: 10px;
      background: #f1f5f9;
      border: 1px solid var(--border);
      color: var(--text-muted);
      font-size: 11px;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.2s ease;
    }

    .chip:hover {
      background: rgba(30, 64, 175, 0.08);
      border-color: rgba(30, 64, 175, 0.2);
      color: var(--primary);
    }

    /* Document Lists */
    .scroll-list {
      display: flex;
      flex-direction: column;
      gap: 8px;
      flex: 1;
      overflow-y: auto;
      padding: 16px 24px;
    }

    .doc-item {
      text-align: left;
      padding: 12px 14px;
      border-radius: 12px;
      border: 1px solid var(--border);
      background: #ffffff;
      cursor: pointer;
      color: var(--text);
      transition: all 0.2s ease;
      display: flex;
      flex-direction: column;
      gap: 4px;
      width: 100%;
    }

    .doc-item:hover, .doc-item.active {
      border-color: var(--primary);
      background: rgba(30, 64, 175, 0.03);
    }

    .doc-item.active {
      border-left: 4px solid var(--accent);
      padding-left: 11px; /* Offset the 4px border */
    }

    .doc-item-title {
      font-family: var(--font-outfit);
      font-weight: 600;
      font-size: 13px;
      color: var(--text);
    }

    .doc-item-meta {
      font-size: 11px;
      color: var(--text-muted);
      display: flex;
      align-items: center;
      gap: 6px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .indicator-tag {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      font-size: 10px;
      font-weight: 700;
      padding: 1px 6px;
      border-radius: 8px;
    }

    .indicator-tag.ready {
      color: var(--success);
      background: rgba(22, 163, 74, 0.08);
    }

    .indicator-tag.pending {
      color: var(--text-muted);
      background: #f1f5f9;
    }

    .dot-pulse {
      width: 5px;
      height: 5px;
      border-radius: 50%;
      background: currentColor;
    }

    /* Search Results */
    .results-container {
      display: flex;
      flex-direction: column;
      gap: 12px;
      overflow-y: auto;
      flex: 1;
      padding: 0 24px 24px 24px;
    }

    .result-item {
      border: 1px solid var(--border);
      background: #ffffff;
      border-radius: 12px;
      padding: 14px;
      transition: all 0.2s ease;
      display: flex;
      flex-direction: column;
      gap: 6px;
    }

    .result-item:hover {
      border-color: var(--primary);
      box-shadow: 0 4px 10px rgba(30, 64, 175, 0.05);
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
      font-size: 13px;
      color: var(--text);
    }

    .result-score {
      font-family: var(--font-mono);
      font-size: 10px;
      color: var(--primary);
      background: rgba(30, 64, 175, 0.08);
      padding: 1px 5px;
      border-radius: 4px;
      font-weight: 700;
      flex-shrink: 0;
    }

    .result-meta {
      font-size: 11px;
      color: var(--text-muted);
      display: flex;
      flex-wrap: wrap;
      gap: 4px 10px;
      border-bottom: 1px dashed var(--border);
      padding-bottom: 6px;
    }

    .result-text {
      font-size: 12.5px;
      color: #334155;
      line-height: 1.5;
    }

    /* Viewer styles */
    .viewer-toolbar {
      padding: 16px 24px;
      border-bottom: 1px solid var(--border);
      display: flex;
      justify-content: space-between;
      align-items: center;
      background: #ffffff;
    }

    .tabs {
      display: flex;
      gap: 4px;
      background: #f1f5f9;
      padding: 3px;
      border-radius: 10px;
      border: 1px solid var(--border);
    }

    .tab {
      padding: 6px 12px;
      border-radius: 7px;
      background: transparent;
      border: none;
      color: var(--text-muted);
      font-family: var(--font-outfit);
      font-weight: 600;
      font-size: 12px;
      cursor: pointer;
      transition: all 0.2s ease;
    }

    .tab.active {
      background: #ffffff;
      color: var(--primary);
      box-shadow: 0 2px 4px rgba(0,0,0,0.05);
      border: 1px solid rgba(0, 0, 0, 0.03);
    }

    .viewport {
      flex: 1;
      overflow-y: auto;
      padding: 24px;
      font-family: var(--font-inter);
      font-size: 13.5px;
      color: #334155;
      line-height: 1.6;
      background: #ffffff;
    }

    .viewport h1, .viewport h2, .viewport h3 {
      font-family: var(--font-outfit);
      color: var(--text);
      font-weight: 700;
      margin-top: 20px;
      margin-bottom: 10px;
    }
    .viewport h1 { font-size: 20px; border-bottom: 1px solid var(--border); padding-bottom: 4px; }
    .viewport h2 { font-size: 17px; }
    .viewport h3 { font-size: 15px; }
    .viewport p { margin-bottom: 12px; }
    .viewport ul, .viewport ol { margin-left: 20px; margin-bottom: 12px; }
    .viewport li { margin-bottom: 4px; }
    
    .viewport blockquote {
      border-left: 4px solid var(--primary);
      padding: 8px 14px;
      background: #f8fafc;
      margin-bottom: 12px;
      border-radius: 0 8px 8px 0;
      color: var(--text-muted);
    }

    .viewport table {
      width: 100%;
      border-collapse: collapse;
      margin-bottom: 12px;
    }
    .viewport th, .viewport td {
      border: 1px solid var(--border);
      padding: 8px 10px;
      text-align: left;
    }
    .viewport th {
      background: #f1f5f9;
      color: var(--text);
      font-weight: 600;
    }

    /* Highlighting */
    mark {
      background: linear-gradient(120deg, rgba(234, 179, 8, 0.2) 0%, rgba(234, 179, 8, 0.35) 100%);
      border-bottom: 2px solid #eab308;
      color: #854d0e;
      padding: 1px 2px;
      border-radius: 3px;
      font-weight: 600;
    }

    /* Diagnostics diagnostic-panel */
    #jsDiagnostics {
      display: none;
    }

    /* Status Dot */
    .status-badge {
      display: flex;
      align-items: center;
      gap: 6px;
      padding: 6px 12px;
      border-radius: 20px;
      background: #ffffff;
      border: 1px solid var(--border);
      font-size: 11px;
      font-weight: 700;
      font-family: var(--font-outfit);
      width: fit-content;
    }
    .status-dot {
      width: 6px;
      height: 6px;
      border-radius: 50%;
      background: #94a3b8;
    }
    .status-badge.online .status-dot {
      background: var(--success);
      box-shadow: 0 0 6px var(--success);
    }
    .status-badge.offline .status-dot {
      background: var(--warning);
      box-shadow: 0 0 6px var(--warning);
    }

    /* Mobile Header & Sidebar toggle */
    .mobile-header {
      display: none;
      background: #ffffff;
      border-bottom: 1px solid var(--border);
      padding: 12px 16px;
      align-items: center;
      justify-content: space-between;
      position: sticky;
      top: 0;
      z-index: 110;
    }

    .mobile-header h1 {
      font-family: var(--font-outfit);
      font-size: 16px;
      font-weight: 800;
      color: var(--primary);
    }

    .sidebar-overlay {
      display: none;
      position: fixed;
      top: 0;
      left: 0;
      right: 0;
      bottom: 0;
      background: rgba(15, 23, 42, 0.4);
      backdrop-filter: blur(2px);
      z-index: 190;
    }

    /* Drop Zone */
    .drop-zone {
      border: 2px dashed var(--border);
      border-radius: 12px;
      padding: 30px 20px;
      text-align: center;
      background: #f8fafc;
      cursor: pointer;
      transition: all 0.2s ease;
      color: var(--text-muted);
      font-size: 13px;
    }

    .drop-zone:hover, .drop-zone.dragover {
      border-color: var(--primary);
      background: rgba(30, 64, 175, 0.03);
      color: var(--primary);
    }

    .browse-link {
      color: var(--primary);
      font-weight: 600;
      text-decoration: underline;
    }

    .fragment-pagination {
      display: flex;
      justify-content: space-between;
      align-items: center;
      width: 100%;
      font-size: 11px;
      color: var(--text-muted);
    }

    .pagination-btns {
      display: flex;
      gap: 6px;
    }

    /* Responsive grid styles */
    @media (max-width: 992px) {
      .app-container {
        flex-direction: column;
      }
      .sidebar-menu {
        position: fixed;
        top: 0;
        left: -280px;
        height: 100vh;
        z-index: 200;
        transition: left 0.25s cubic-bezier(0.4, 0, 0.2, 1);
        box-shadow: 8px 0 25px rgba(15, 23, 42, 0.1);
      }
      .sidebar-menu.open {
        left: 0;
      }
      .sidebar-overlay.open {
        display: block;
      }
      .mobile-header {
        display: flex;
      }
      main {
        flex-direction: column;
      }
      .col-content, .col-viewer {
        width: 100% !important;
        display: none !important;
      }
      .col-content.mobile-visible, .col-viewer.mobile-visible {
        display: flex !important;
      }
      .card {
        margin: 12px 16px 0 16px;
      }
      .card:last-child {
        margin-bottom: 16px;
      }
      .scroll-list, .results-container {
        padding: 0 16px 16px 16px;
      }
    }
  </style>
</head>
<body>
  <noscript>
    <div style="position: fixed; top: 0; left: 0; right: 0; background: #ef4444; color: white; padding: 16px; font-family: sans-serif; font-size: 14px; z-index: 999999; text-align: center; font-weight: bold; box-shadow: 0 4px 15px rgba(0,0,0,0.5);">
      &#9888; JavaScript esta desactivado o bloqueado en tu navegador. Para utilizar Busca Ordenanza, activa JavaScript en la configuracion del navegador o desactiva extensiones de bloqueo para este sitio.
    </div>
  </noscript>

  <!-- Floating Diagnostic Panel (hidden compatibility element) -->
  <div id="jsDiagnostics" style="display: none;">
    <span id="diagScript">SÍ</span>
    <span id="diagDOM">SÍ</span>
    <span id="diagInit">SÍ</span>
    <span id="diagLoadDocs">SÍ</span>
    <div id="diagErrorBox"></div>
  </div>

  <div id="resultBox" style="display: none;"></div>

  <div class="app-container">
    
    <!-- Mobile Header -->
    <div class="mobile-header">
      <button id="mobileMenuToggle" class="btn-secondary btn-icon" style="padding: 6px;">
        <svg width="20" height="20" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 6h16M4 12h16M4 18h16" />
        </svg>
      </button>
      <h1>Busca Ordenanza</h1>
      <div id="mobileTabs" class="tabs">
        <button id="mobileTabList" class="tab active">Lista</button>
        <button id="mobileTabViewer" class="tab">Visor</button>
      </div>
    </div>

    <!-- Sidebar Menu Overlay for Mobile -->
    <div id="sidebarOverlay" class="sidebar-overlay"></div>

    <!-- Sidebar Menu (Left panel) -->
    <aside id="sidebarMenu" class="sidebar-menu">
      <div class="sidebar-brand">
        <h1>Busca Ordenanza</h1>
      </div>
      
      <div id="meiliStatus" class="status-badge" style="margin-top: -8px;">
        <span class="status-dot"></span>
        <span id="meiliStatusText">Comprobando...</span>
      </div>
      
      <div class="menu-section">
        <span class="menu-section-title">BÚSQUEDA</span>
        <button id="menuBtnSearch" class="menu-item active">
          <svg width="18" height="18" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
          </svg>
          <span>Buscar Global</span>
        </button>
      </div>
      
      <div class="menu-section">
        <span class="menu-section-title">DOCUMENTOS</span>
        <button id="menuBtnOrd" class="menu-item" data-category="Ordenanzas">
          <svg width="18" height="18" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
          </svg>
          <span>Ordenanzas</span>
          <span id="badgeOrd" class="menu-badge">0</span>
        </button>
        <button id="menuBtnAlc" class="menu-item" data-category="Resoluciones de Alcaldía">
          <svg width="18" height="18" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
          </svg>
          <span>Res. de Alcaldía</span>
          <span id="badgeAlc" class="menu-badge">0</span>
        </button>
        <button id="menuBtnCon" class="menu-item" data-category="Resoluciones de Concejo">
          <svg width="18" height="18" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" />
          </svg>
          <span>Res. de Concejo</span>
          <span id="badgeCon" class="menu-badge">0</span>
        </button>
      </div>

      <div class="menu-section" style="margin-top: auto;">
        <span class="menu-section-title">ADMINISTRACIÓN</span>
        <button id="menuBtnUpload" class="menu-item">
          <svg width="18" height="18" fill="none" viewBox="0 0 24 24" stroke="currentColor">
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12" />
          </svg>
          <span>Subir Archivos</span>
        </button>
      </div>
    </aside>

    <!-- Main Body Content Grid -->
    <main>
      
      <!-- Middle Column Panel: Search View, Category View OR Upload View -->
      <div id="colContent" class="col-content mobile-visible">
        
        <!-- A. VIEW 1: Search Form and Query panel (Active by default) -->
        <div id="searchPanel" style="display: flex; flex-direction: column; height: 100%;">
          <div class="panel-header">
            <h2>Buscar Global</h2>
            <p>Realiza búsquedas en el contenido de todos los documentos.</p>
          </div>
          
          <section class="card">
            <span class="pill-badge">Búsqueda Semántica</span>
            <h3 class="card-title">Consultar</h3>
            <p class="description-text" style="margin-bottom: 12px;">Busca de forma instantánea en todas las ordenanzas de la biblioteca.</p>
            
            <form id="searchForm" class="input-wrapper">
              <input id="query" name="query" type="text" placeholder="Palabras clave a buscar..." required>
              <button type="submit" class="btn-primary">
                <svg width="15" height="15" fill="none" viewBox="0 0 24 24" stroke="currentColor">
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

          <!-- Results list -->
          <div class="panel-header" style="border: none; padding-top: 14px; padding-bottom: 10px;">
            <h2>Resultados de Búsqueda</h2>
          </div>
          <div class="results-container" id="searchResults">
            <p class="description-text" style="text-align: center; font-style: italic; margin-top: 10px;">Escribe algo y presiona Buscar para consultar los índices.</p>
          </div>
        </div>

        <!-- B. VIEW 2: Category Document List panel (Hidden by default) -->
        <div id="categoryPanel" style="display: none; flex-direction: column; height: 100%;">
          <div class="panel-header">
            <div style="display: flex; justify-content: space-between; align-items: center; width: 100%;">
              <h2 id="categoryTitle">Documentos</h2>
              <button id="refreshDocs" class="btn-secondary btn-icon" title="Actualizar lista" style="padding: 6px; border-radius: 50%; width: 30px; height: 30px; display: flex; align-items: center; justify-content: center;">
                <svg width="14" height="14" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 4v5h.582m15.356 2A8.001 8.001 0 1121.21 8H18.5" />
                </svg>
              </button>
            </div>
            <p>Listado completo de archivos en esta categoría.</p>
            
            <!-- Document list inline text filter -->
            <div class="input-wrapper" style="margin-top: 8px;">
              <input id="categorySearchInput" type="text" placeholder="Filtrar documentos por nombre o título...">
            </div>
          </div>
          
          <div class="scroll-list" id="docList">
            <p class="description-text" style="text-align: center; font-style: italic;">Cargando categoría...</p>
          </div>
        </div>

        <!-- C. VIEW 3: Upload Panel (Hidden by default) -->
        <div id="uploadPanel" style="display: none; flex-direction: column; height: 100%;">
          <div class="panel-header">
            <h2>Subir Archivos</h2>
            <p>Cargar nuevas ordenanzas o resoluciones en formato PDF.</p>
          </div>
          
          <section class="card">
            <span class="pill-badge">Administración</span>
            <h3 class="card-title">Cargar Documentos</h3>
            <p class="description-text">Sube archivos PDF en lote para procesarlos automáticamente e indexarlos.</p>
            
            <form id="uploadForm" action="/subida-archivos" method="post" enctype="multipart/form-data" style="display: flex; flex-direction: column; gap: 12px;">
              <div class="drop-zone" id="dropZone">
                <span class="drop-zone-text">Arrastra archivos aquí o <span class="browse-link">busca en tu PC</span></span>
                <input type="file" name="files" id="files" multiple accept=".pdf" style="display: none;">
              </div>
              
              <button type="submit" id="uploadButton" class="btn-primary" style="width: 100%;">
                <svg width="16" height="16" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12" />
                </svg>
                <span>Subir y Procesar</span>
              </button>
            </form>
            <div id="uploadStatus" class="description-text" style="margin-top: 10px; font-weight: 500; font-size: 12px; text-align: center;"></div>
          </section>
        </div>

      </div>

      <!-- Right Column Panel: Document Viewer -->
      <div id="colViewer" class="col-viewer">
        <div class="viewer-header" style="display: flex; flex-direction: column; height: 100%;">
          <div class="viewer-toolbar">
            <div>
              <h2 class="card-title" id="viewerTitle" style="margin-bottom: 2px;">Visor de Documentos</h2>
              <p id="viewerMetaText" class="description-text" style="margin-bottom: 0; font-size: 12px;">Selecciona una ordenanza o resolución para comenzar.</p>
            </div>
            
            <div style="display: flex; gap: 8px;">
              <button id="downloadOriginalPDF" class="btn-secondary" disabled>
                <span>Descargar PDF</span>
                <svg width="12" height="12" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
                </svg>
              </button>
              <button id="openRawMarkdown" class="btn-secondary" disabled>
                <span>Crudo</span>
                <svg width="12" height="12" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                  <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
                </svg>
              </button>
            </div>
          </div>

          <div class="viewer-toolbar" style="border: none; padding: 10px 24px; background: #f8fafc;">
            <div class="tabs">
              <button id="tabComplete" class="tab active">Lectura Completa</button>
              <button id="tabFragment" class="tab" disabled>Fragmento Seleccionado</button>
              <button id="tabPDF" class="tab" disabled>PDF Original</button>
            </div>
            <div id="viewerLoading" class="loading-pulse" style="display: none; border-top-color: var(--primary);"></div>
          </div>

          <!-- Viewport -->
          <div class="viewport" id="markdownViewer">
            <div style="height: 100%; display: flex; flex-direction: column; justify-content: center; align-items: center; text-align: center; padding: 40px 0;">
              <svg width="40" height="40" fill="none" viewBox="0 0 24 24" stroke="currentColor" style="color: var(--text-muted); opacity: 0.4; margin-bottom: 12px;">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="1.5" d="M12 6.253v13m0-13C10.832 5.477 9.246 5 7.5 5S4.168 5.477 3 6.253v13C4.168 18.477 5.754 18 7.5 18s3.332.477 4.5 1.253m0-13C13.168 5.477 14.754 5 16.5 5c1.747 0 3.332.477 4.5 1.253v13C19.832 18.477 18.247 18 16.5 18c-1.746 0-3.332.477-4.5 1.253" />
              </svg>
              <h3 style="font-family: var(--font-outfit); font-weight: 600; color: var(--text); margin-bottom: 4px;">Ningún documento activo</h3>
              <p class="description-text" style="max-width: 320px; font-size: 13px;">Selecciona una ordenanza o resolución de las categorías del menú lateral, o realiza una búsqueda para visualizar su contenido.</p>
            </div>
          </div>
          
          <!-- Viewer Fragment Search Bar inside markdown content viewer -->
          <div id="viewerFragmentBar" class="viewer-toolbar" style="border-top: 1px solid var(--border); display: none; padding: 12px 24px; gap: 10px;">
            <input id="fragmentQuery" type="text" placeholder="Filtrar fragmentos..." style="flex: 1; padding: 8px 12px; font-size: 12px;">
            <button id="fragmentSearch" class="btn-primary" style="padding: 8px 14px; font-size: 12px;">Filtrar</button>
            <button id="fragmentClear" class="btn-secondary" style="padding: 8px 14px; font-size: 12px;">Limpiar</button>
          </div>
          
          <!-- Fragment results section in visor panel -->
          <div id="viewerFragmentSection" class="viewer-toolbar" style="border-top: 1px solid var(--border); display: none; flex-direction: column; padding: 12px 24px; gap: 8px;">
            <div class="fragment-pagination">
              <span id="fragmentPageInfo">Cargando fragmentos...</span>
              <div class="pagination-btns">
                <button id="fragmentPrev" class="btn-secondary" style="padding: 4px 8px; font-size: 11px;">Prev</button>
                <button id="fragmentNext" class="btn-secondary" style="padding: 4px 8px; font-size: 11px;">Sig</button>
              </div>
            </div>
            <div class="fragment-list" id="fragmentList" style="width: 100%; display: flex; flex-direction: row; gap: 8px; overflow-x: auto; max-height: none; padding: 4px 0;">
              <!-- fragments will slide horizontally -->
            </div>
            <div id="fragCountBadge" style="display: none;">0</div>
          </div>
        </div>
      </div>
    </main>
  </div>

  <script src="/api/app.js"></script>
  <script>
    document.addEventListener('DOMContentLoaded', () => {
      // Toggle sidebar drawer on mobile
      const mobileMenuToggle = document.getElementById('mobileMenuToggle');
      const sidebarMenu = document.getElementById('sidebarMenu');
      const sidebarOverlay = document.getElementById('sidebarOverlay');

      function toggleSidebar() {
        sidebarMenu.classList.toggle('open');
        sidebarOverlay.classList.toggle('open');
      }

      if (mobileMenuToggle && sidebarOverlay) {
        mobileMenuToggle.addEventListener('click', toggleSidebar);
        sidebarOverlay.addEventListener('click', toggleSidebar);
      }

      // Handle mobile views tab switcher
      const mobileTabList = document.getElementById('mobileTabList');
      const mobileTabViewer = document.getElementById('mobileTabViewer');
      const colContent = document.getElementById('colContent');
      const colViewer = document.getElementById('colViewer');

      function showMobileView(viewName) {
        if (!mobileTabList || !mobileTabViewer) return;

        mobileTabList.classList.remove('active');
        mobileTabViewer.classList.remove('active');
        colContent.classList.remove('mobile-visible');
        colViewer.classList.remove('mobile-visible');

        if (viewName === 'list') {
          mobileTabList.classList.add('active');
          colContent.classList.add('mobile-visible');
        } else if (viewName === 'viewer') {
          mobileTabViewer.classList.add('active');
          colViewer.classList.add('mobile-visible');
        }
      }

      if (mobileTabList && mobileTabViewer) {
        mobileTabList.addEventListener('click', () => showMobileView('list'));
        mobileTabViewer.addEventListener('click', () => showMobileView('viewer'));
      }

      // Close sidebar drawer when menu item is clicked on mobile
      document.querySelectorAll('.menu-item').forEach(item => {
        item.addEventListener('click', () => {
          if (sidebarMenu.classList.contains('open')) {
            toggleSidebar();
          }
          // On mobile, show list view when clicking a sidebar item
          showMobileView('list');
        });
      });

      // Switch to viewer tab when document is loaded
      window.addEventListener('switch-to-viewer', () => {
        showMobileView('viewer');
      });
    });
  </script>
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
    from fastapi.responses import RedirectResponse, FileResponse
    
    s3_client = get_s3_client()
    if s3_client:
        # R2 mode: generate presigned URL
        try:
            response = s3_client.list_objects_v2(Bucket=settings.bucket_name, Prefix=f"uploads/{document_id}")
            matching_key = None
            for obj in response.get('Contents', []):
                key = obj['Key']
                if Path(key).stem == document_id:
                    matching_key = key
                    break

            if not matching_key:
                raise HTTPException(status_code=404, detail="PDF no encontrado en R2")

            # Si el documento original no es un PDF (por ejemplo, Word .doc/.docx), devolvemos el HTML de advertencia
            suffix = Path(matching_key).suffix.lower()
            if suffix != '.pdf':
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

            # Determinar si es descarga o previsualización
            response_params = {}
            if download:
                original_filename = Path(matching_key).name
                parts = original_filename.split('_', 1)
                if len(parts) > 1 and len(parts[0]) == 32 and all(c in '0123456789abcdefABCDEF' for c in parts[0]):
                    display_name = parts[1]
                else:
                    display_name = original_filename
                response_params['ResponseContentDisposition'] = f'attachment; filename="{display_name}"'
            else:
                response_params['ResponseContentDisposition'] = 'inline'

            presigned_url = s3_client.generate_presigned_url(
                'get_object',
                Params={
                    'Bucket': settings.bucket_name,
                    'Key': matching_key,
                    **response_params
                },
                ExpiresIn=900
            )
            return RedirectResponse(url=presigned_url, status_code=307)
        except Exception as e:
            print(f"Error generando URL firmada para {document_id}: {e}")
            raise HTTPException(status_code=500, detail=f"Error al obtener PDF desde la nube: {e}")
    else:
        # Fallback local
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
        
        # Invalidate the cache for this document
        doc_id = Path(result['markdown_path']).stem
        _fragments_cache.pop(doc_id, None)

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
    if _meili_online:
        try:
            service = MeiliService.from_settings()
            results = service.search(payload.query, payload.limit)
            return {
                'query': payload.query,
                'limit': payload.limit,
                'results': results,
                'message': 'Resultados obtenidos desde Meilisearch',
            }
        except Exception:
            pass

    # Fallback to serverless in-memory search index
    local_hits = []
    try:
        index_data = get_in_memory_index()
        if index_data:
            query_str = payload.query.strip().lower()
            parsed_query = parse_search_query(query_str) if query_str else []
            
            if parsed_query:
                phrase_term = next((t['text'] for t in parsed_query if t['is_phrase']), None)
                words_terms = [t for t in parsed_query if not t['is_phrase']]
                required_words = [t['text'] for t in words_terms if t['required']]
                
                for chunk in index_data:
                    haystack = chunk.get('_haystack')
                    if haystack is None:
                        # Fallback if not precalculated
                        haystack = f"{chunk.get('title', '')} {chunk.get('section', '')} {chunk.get('content_text', '')}".lower()
                        chunk['_haystack'] = haystack
                    
                    # Apply matching filter: all required terms must be present
                    matched_all = True
                    for word in required_words:
                        if word not in haystack:
                            matched_all = False
                            break
                    if not matched_all:
                        continue
                    
                    # Calculate score
                    score = 0
                    matched_count = 0
                    for t in words_terms:
                        cnt = haystack.count(t['text'])
                        if cnt > 0:
                            matched_count += 1
                            score += min(cnt, 5) * t['weight']
                            
                    if phrase_term and phrase_term in haystack:
                        score += haystack.count(phrase_term) * 50000
                        
                    if matched_count > 0:
                        score += matched_count * 10000
                        
                    if score > 0:
                        local_hits.append({
                            'id': chunk['id'],
                            'document_id': chunk['document_id'],
                            'title': chunk['title'],
                            'section': chunk['section'],
                            'chunk_number': chunk['chunk_number'],
                            'source': chunk['source'],
                            'content_markdown': '',
                            'content_text': chunk['content_text'],
                            'score': score
                        })
                
                # Sort results by score descending, then by chunk number
                local_hits.sort(key=lambda item: (-item['score'], item['chunk_number']))
                local_hits = local_hits[:payload.limit]
                message = "Resultados locales indexados en memoria (Meilisearch desconectado)"
            else:
                message = "Consulta vacía"
        else:
            message = "El indice de busqueda en memoria esta vacio o no se pudo cargar"
    except Exception as local_err:
        message = f"Busqueda serverless fallo: {local_err}"
        
    return {
        'query': payload.query,
        'limit': payload.limit,
        'results': {'hits': local_hits},
        'message': message,
    }
