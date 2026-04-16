import os

MAX_FILES_PER_MESSAGE = 20
MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024
MAX_FILE_SIZE_MB = MAX_FILE_SIZE_BYTES / (1024 * 1024)

SIGNED_URL_EXPIRY_SECONDS = 3600
MAX_PDF_PAGES_BLOCKING = 50
MAX_TABULAR_ROWS_PROFILE = 50_000
MAX_TABULAR_COLUMNS = 200
MAX_IMAGE_MEGAPIXELS = 25
MAX_TEXT_BYTES = 5 * 1024 * 1024

ALLOWED_EXTENSIONS = frozenset(
    {
        '.jpg',
        '.jpeg',
        '.png',
        '.gif',
        '.webp',
        '.bmp',
        '.tiff',
        '.svg',
        '.pdf',
        '.doc',
        '.docx',
        '.txt',
        '.csv',
        '.tsv',
        '.xlsx',
    }
)

ALLOWED_CONTENT_TYPES = frozenset(
    {
        'image/jpeg',
        'image/png',
        'image/gif',
        'image/webp',
        'image/bmp',
        'image/tiff',
        'image/svg+xml',
        'application/pdf',
        'application/msword',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'text/plain',
        'text/csv',
        'application/csv',
        'text/tab-separated-values',
        'application/vnd.ms-excel',
        'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    }
)


_CONTENT_TYPE_TO_CATEGORY = {
    'image/': 'image',
    'application/pdf': 'document',
    'application/msword': 'word',
    'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'word',
    'text/plain': 'text',
    'text/csv': 'spreadsheet',
    'application/csv': 'spreadsheet',
    'text/tab-separated-values': 'spreadsheet',
    'application/vnd.ms-excel': 'spreadsheet',
    'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'spreadsheet',
}


_EXTENSION_TO_CATEGORY = {
    '.jpg': 'image',
    '.jpeg': 'image',
    '.png': 'image',
    '.gif': 'image',
    '.webp': 'image',
    '.bmp': 'image',
    '.tiff': 'image',
    '.svg': 'image',
    '.pdf': 'document',
    '.doc': 'word',
    '.docx': 'word',
    '.txt': 'text',
    '.csv': 'spreadsheet',
    '.tsv': 'spreadsheet',
    '.xlsx': 'spreadsheet',
}


def resolve_file_category(content_type: str, filename: str) -> str:
    ct = content_type.lower().strip()

    for key, category in _CONTENT_TYPE_TO_CATEGORY.items():
        if ct == key or ct.startswith(key):
            return category

    ext = os.path.splitext(filename)[1].lower()
    if ext in _EXTENSION_TO_CATEGORY:
        return _EXTENSION_TO_CATEGORY[ext]

    return 'document'
