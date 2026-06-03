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

    return chunks
