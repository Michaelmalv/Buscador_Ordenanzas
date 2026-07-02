import os
import sys
import json
import time
import gzip
import uuid
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import settings
from ingestion.workflow import get_s3_client
from ingestion.markdown_chunker import markdown_to_plain_text, split_markdown_by_headers

def process_markdown_file(args):
    s3_client, bucket_name, key = args
    try:
        # 1. Download markdown text from R2
        response = s3_client.get_object(Bucket=bucket_name, Key=key)
        markdown_text = response['Body'].read().decode('utf-8')
        
        document_id = Path(key).stem
        source_name = Path(key).name
        title = document_id.replace('_', ' ').replace('-', ' ').title()
        
        # 2. Split into small sub-chunks
        chunks = split_markdown_by_headers(markdown_text)
        
        # 3. Build document chunks for both local search / view
        document_chunks = []
        clean_chunks = [] # without markdown content, for global search index
        
        for index, chunk in enumerate(chunks, start=1):
            # Do NOT prepend the section header to the content. It is already stored in the 'section' field.
            # This ensures card previews display the actual matching paragraph.
            content_markdown = chunk['content_markdown'].strip()
            if not content_markdown:
                continue
                
            chunk_id = f"{document_id}-fragment-{index}"
            content_text = markdown_to_plain_text(content_markdown)
            
            # Full chunk payload for individual JSON (used by viewer)
            doc_chunk = {
                'id': chunk_id,
                'document_id': document_id,
                'title': title,
                'section': chunk['section'],
                'chunk_number': index,
                'source': source_name,
                'content_markdown': content_markdown,
                'content_text': content_text,
            }
            document_chunks.append(doc_chunk)
            
            # Clean chunk payload for global search index (lightweight)
            clean_chunk = {
                'id': chunk_id,
                'document_id': document_id,
                'title': title,
                'section': chunk['section'],
                'chunk_number': index,
                'source': source_name,
                'content_text': content_text
            }
            clean_chunks.append(clean_chunk)
            
        # 4. Upload updated JSON chunks file to R2
        json_key = f"json/{document_id}.json"
        s3_client.put_object(
            Bucket=bucket_name,
            Key=json_key,
            Body=json.dumps(document_chunks, ensure_ascii=False, indent=2),
            ContentType="application/json"
        )
        
        return clean_chunks
    except Exception as e:
        print(f"[ERROR] Procesando {key}: {e}")
        return []

def main():
    print("=" * 60)
    print("      RECONSTRUCTOR DE INDICE SERVERLESS EN MEMORIA       ")
    print("=" * 60)
    sys.stdout.flush()

    # 1. Conectar con Cloudflare R2
    s3_client = get_s3_client()
    if not s3_client:
        print("[ERROR] No se pudieron cargar las credenciales de R2.")
        return

    print("[OK] Conexion con Cloudflare R2 establecida.")
    sys.stdout.flush()

    # 2. Listar archivos Markdown en R2
    print("\nListando archivos Markdown en el bucket de R2...")
    sys.stdout.flush()
    try:
        paginator = s3_client.get_paginator('list_objects_v2')
        pages = paginator.paginate(Bucket=settings.bucket_name, Prefix="markdown/")
        md_keys = []
        for page in pages:
            if 'Contents' in page:
                for obj in page['Contents']:
                    key = obj['Key']
                    if key.endswith('.md'):
                        md_keys.append(key)
        print(f"Se encontraron {len(md_keys)} archivos Markdown en Cloudflare R2.")
        sys.stdout.flush()
    except Exception as e:
        print(f"[ERROR] Fallo el listado de R2: {e}")
        return

    if not md_keys:
        print("No hay archivos Markdown en R2.")
        return

    # 3. Procesar, re-segmentar y subir en paralelo
    print(f"\nRe-segmentando y actualizando JSONs individuales (usando 30 hilos)...")
    sys.stdout.flush()
    
    consolidated_index = []
    task_args = [(s3_client, settings.bucket_name, key) for key in md_keys]
    
    start_time = time.time()
    with ThreadPoolExecutor(max_workers=30) as executor:
        futures = {executor.submit(process_markdown_file, arg): arg for arg in task_args}
        for idx, future in enumerate(as_completed(futures), 1):
            chunks = future.result()
            consolidated_index.extend(chunks)
            if idx % 500 == 0 or idx == len(md_keys):
                print(f"  --> Procesados {idx}/{len(md_keys)} archivos Markdown...")
                sys.stdout.flush()

    duration = time.time() - start_time
    print(f"[OK] Re-segmentacion completada. Total de nuevos sub-fragmentos: {len(consolidated_index)}")
    sys.stdout.flush()

    # 4. Guardar archivo local comprimido GZIP
    local_index_path = ROOT_DIR / "search_index.json.gz"
    print(f"\nComprimiendo y guardando indice local en {local_index_path}...")
    sys.stdout.flush()
    try:
        with gzip.open(local_index_path, "wt", encoding="utf-8") as f:
            json.dump(consolidated_index, f, ensure_ascii=False)
        
        file_size_mb = os.path.getsize(local_index_path) / (1024 * 1024)
        print(f"[OK] Indice local comprimido guardado. Tamano: {file_size_mb:.2f} MB.")
        sys.stdout.flush()
    except Exception as e:
        print(f"[ERROR] No se pudo guardar el archivo local: {e}")
        return

    # 5. Subir indice consolidado a R2
    r2_key = "search_index.json.gz"
    print(f"\nSubiendo '{r2_key}' a Cloudflare R2...")
    sys.stdout.flush()
    try:
        s3_client.upload_file(
            Filename=str(local_index_path),
            Bucket=settings.bucket_name,
            Key=r2_key,
            ExtraArgs={"ContentType": "application/gzip"}
        )
        print("[OK] Indice de busqueda (GZIP) reconstruido y subido a R2 con exito.")
        print("-" * 60)
        print("   PROCESO TERMINADO: Todo el buscador ha sido re-indexado con fragmentos optimizados.")
        print("=" * 60)
        sys.stdout.flush()
    except Exception as e:
        print(f"[ERROR] Fallo la subida a R2: {e}")

if __name__ == '__main__':
    main()
