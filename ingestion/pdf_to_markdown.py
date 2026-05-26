import os
from pathlib import Path
import pymupdf4llm
import fitz  # Importar PyMuPDF estándar


def extract_fallback_markdown(input_path: str) -> str:
    """
    Extractor de texto robusto y ultrarrápido usando PyMuPDF estándar.
    Garantiza que no se pierda nada de texto en páginas con planos, mapas o vectores complejos.
    """
    doc = fitz.open(input_path)
    md_text = []
    for page in doc:
        md_text.append(f"\n\n## Página {page.number + 1}\n\n")
        text = page.get_text("text")
        if text.strip():
            md_text.append(text)
        else:
            md_text.append("*[Página sin texto legible extraíble]*\n")
    return "".join(md_text)


def convert_to_md(input_path: str, output_dir: str) -> str:
    """
    Convierte un documento PDF a Markdown usando la biblioteca de código abierto pymupdf4llm.
    Incluye un mecanismo de fallback inteligente para evitar omisión de texto en PDFs técnicos.
    """
    os.makedirs(output_dir, exist_ok=True)

    file_name = Path(input_path).stem
    output_path = os.path.join(output_dir, f'{file_name}.md')

    # 1. Intentar conversión estructurada con pymupdf4llm
    try:
        md_text = pymupdf4llm.to_markdown(input_path, use_ocr=False)
    except Exception as e:
        print(f"Advertencia en to_markdown para {file_name}: {e}. Usando fallback.")
        md_text = ""

    # 2. Control de calidad: ¿Se omitió el texto debido a planos o vectores complejos?
    # Limpiar el markdown generado de los marcadores de imágenes omitidas
    clean_md = md_text.replace("intentionally omitted", "").replace("picture", "").replace("==>", "").replace("<==", "")
    clean_md = "".join(line for line in clean_md.splitlines() if line.strip() and not line.strip().startswith("#"))

    # Obtener el texto nativo completo por vía de PyMuPDF directo
    try:
        fallback_text = extract_fallback_markdown(input_path)
    except Exception as e:
        print(f"Error en extractor fallback para {file_name}: {e}")
        fallback_text = ""

    # Si el markdown de to_markdown quedó casi vacío (menos de 150 caracteres de texto real)
    # pero el PDF sí tiene texto nativo legible significativo, activamos la salvaguarda.
    if len(clean_md.strip()) < 150 and len(fallback_text.strip()) > 300:
        print(f"--> ¡Activado Fallback Inteligente para {file_name}! (Evitando omisión por planos o mapas)")
        md_text = fallback_text

    # Escribir el contenido a archivo con codificación UTF-8
    with open(output_path, 'w', encoding='utf-8') as file:
        file.write(md_text)

    return output_path


def batch_convert_to_md(input_directory: str, output_dir: str) -> list[str]:
    """
    Convierte en lote todos los archivos PDF/DOC/DOCX de un directorio a Markdown.
    """
    if not os.path.exists(input_directory):
        raise FileNotFoundError(f"La carpeta '{input_directory}' no existe.")

    supported_extensions = ['.pdf', '.doc', '.docx']
    input_files = [
        f for f in os.listdir(input_directory)
        if any(f.lower().endswith(ext) for ext in supported_extensions)
    ]

    output_paths: list[str] = []
    for input_file in input_files:
        input_path = os.path.join(input_directory, input_file)
        try:
            output_paths.append(convert_to_md(input_path, output_dir))
        except Exception as e:
            print(f"Error convirtiendo {input_file}: {e}")

    return output_paths
