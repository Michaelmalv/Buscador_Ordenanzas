import shutil
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
MARKDOWN_DIR = ROOT_DIR / 'data' / 'markdown'
JSON_DIR = ROOT_DIR / 'data' / 'json'

def main():
    print("=" * 60)
    print(" LIMPIEZA DE CACHÉ DE DOCUMENTOS TRUNCADOS ")
    print("=" * 60)
    
    deleted_md = 0
    deleted_json = 0
    
    if MARKDOWN_DIR.exists():
        for file in MARKDOWN_DIR.iterdir():
            if file.is_file() and file.suffix.lower() == '.md':
                try:
                    file.unlink()
                    deleted_md += 1
                except Exception as e:
                    print(f"Error eliminando {file.name}: {e}")
                    
    if JSON_DIR.exists():
        for file in JSON_DIR.iterdir():
            if file.is_file() and file.suffix.lower() == '.json':
                try:
                    file.unlink()
                    deleted_json += 1
                except Exception as e:
                    print(f"Error eliminando {file.name}: {e}")
                    
    print("-" * 60)
    print(f"Archivos Markdown (.md) eliminados: {deleted_md}")
    print(f"Archivos JSON (.json) de índice eliminados: {deleted_json}")
    print("=" * 60)
    print("¡Limpieza completada con éxito!")
    print("Ahora puedes ejecutar 'python scripts/procesar_biblioteca.py' para regenerar todo.")
    print("=" * 60)

if __name__ == '__main__':
    main()
