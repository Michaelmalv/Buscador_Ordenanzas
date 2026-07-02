import re
from typing import Any


def markdown_to_plain_text(markdown: str) -> str:
    text = markdown
    # Replace HTML line breaks (literal or escaped) with space to improve phrase matching
    text = re.sub(r'<br\s*/?>|&lt;br\s*/?&gt;', ' ', text, flags=re.IGNORECASE)
    text = re.sub(r'```.*?```', ' ', text, flags=re.DOTALL)
    text = re.sub(r'`([^`]*)`', r'\1', text)
    text = re.sub(r'!\[.*?\]\(.*?\)', ' ', text)
    text = re.sub(r'\[(.*?)\]\(.*?\)', r'\1', text)
    text = re.sub(r'^#{1,6}\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'[*_~>#-]', ' ', text)
    text = re.sub(r'\n{2,}', '\n', text)
    text = re.sub(r'[ \t]+', ' ', text)
    return text.strip()


def split_markdown_by_headers(markdown: str) -> list[dict[str, Any]]:
    lines = markdown.splitlines()
    chunks = []
    current_title = 'Inicio'
    current_lines = []
    header_pattern = re.compile(r'^(#{1,6})\s+(.*)$')

    for line in lines:
        match = header_pattern.match(line)
        if match:
            if current_lines:
                chunks.append({
                    'section': current_title,
                    'content_markdown': '\n'.join(current_lines).strip(),
                })
                current_lines = []
            current_title = match.group(2).strip()
        current_lines.append(line)

    if current_lines:
        chunks.append({
            'section': current_title,
            'content_markdown': '\n'.join(current_lines).strip(),
        })

    # Sub-split chunks that are too large (e.g., > 2000 characters)
    MAX_CHUNK_SIZE = 2000
    final_chunks = []
    
    for chunk in chunks:
        content = chunk['content_markdown']
        if len(content) <= MAX_CHUNK_SIZE:
            final_chunks.append(chunk)
            continue
            
        # Sub-split content by paragraphs (double newlines) to preserve semantic units
        paragraphs = content.split('\n\n')
        current_sub_lines = []
        current_sub_len = 0
        part_idx = 1
        
        for para in paragraphs:
            para = para.strip()
            if not para:
                continue
            
            # If a single paragraph is exceptionally large, split it by lines instead
            if len(para) > MAX_CHUNK_SIZE:
                lines_in_para = para.split('\n')
                for line in lines_in_para:
                    line = line.strip()
                    if not line:
                        continue
                    if current_sub_len + len(line) > MAX_CHUNK_SIZE and current_sub_lines:
                        final_chunks.append({
                            'section': f"{chunk['section']} (Parte {part_idx})",
                            'content_markdown': '\n'.join(current_sub_lines),
                        })
                        current_sub_lines = []
                        current_sub_len = 0
                        part_idx += 1
                    current_sub_lines.append(line)
                    current_sub_len += len(line) + 1
                continue
            
            if current_sub_len + len(para) > MAX_CHUNK_SIZE and current_sub_lines:
                final_chunks.append({
                    'section': f"{chunk['section']} (Parte {part_idx})",
                    'content_markdown': '\n\n'.join(current_sub_lines),
                })
                current_sub_lines = []
                current_sub_len = 0
                part_idx += 1
            
            current_sub_lines.append(para)
            current_sub_len += len(para) + 2
            
        if current_sub_lines:
            final_chunks.append({
                'section': f"{chunk['section']} (Parte {part_idx})" if part_idx > 1 else chunk['section'],
                'content_markdown': '\n\n'.join(current_sub_lines),
            })
            
    return final_chunks
