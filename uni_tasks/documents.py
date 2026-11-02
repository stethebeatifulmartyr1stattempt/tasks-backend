"""Bounded, text-only document extraction in a disposable subprocess."""
import io
import json
import sys
import zipfile
from xml.etree import ElementTree

MAX_BYTES = 8 * 1024 * 1024
MAX_TEXT = 24000


def extract(data, kind):
    if len(data) > MAX_BYTES:
        raise ValueError('Document is too large')
    truncated = False
    if kind == 'docx':
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            info = archive.getinfo('word/document.xml')
            if info.file_size > MAX_BYTES:
                raise ValueError('Expanded document is too large')
            tree = ElementTree.fromstring(archive.read(info))
        ns = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
        text = '\n'.join(''.join(p.itertext()) for p in tree.iter(ns + 'p'))
    elif kind == 'pdf':
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ValueError('Encrypted PDF')
        chunks = []
        length = 0
        for index, page in enumerate(reader.pages):
            if index >= 150 or length > MAX_TEXT:
                truncated = True
                break
            chunk = page.extract_text() or ''
            chunks.append(chunk)
            length += len(chunk)
        text = '\n'.join(chunks)
    else:
        raise ValueError('Unsupported document type')
    if not text.strip():
        raise ValueError('Document has no extractable text')
    return {'text': text[:MAX_TEXT], 'truncated': truncated or len(text) > MAX_TEXT}


if __name__ == '__main__':
    # Limits supplement the worker's container memory cap. No macros, links or
    # embedded programs are executed; only PDF text / document XML is read.
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (384 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
    except ImportError:
        pass
    print(json.dumps(extract(sys.stdin.buffer.read(MAX_BYTES + 1), sys.argv[1])))
