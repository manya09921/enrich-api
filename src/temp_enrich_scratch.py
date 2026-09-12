# Scratchpad for testing book enrichment schema and Ollama retry logic
ALLOWED_CATEGORIES = ['fiction', 'non-fiction', 'poetry', 'science', 'history', 'other']
TIMEOUT_SECONDS = 30
MAX_RETRIES = 2
def check_quality_flags(record: dict) -> list:
    flags = []
    if not record.get('description'): flags.append('missing_description')
    if record.get('price_gbp', 0) <= 0: flags.append('invalid_price')
    return flags
def should_retry_status(status_code: int) -> bool:
    return status_code in (429, 500, 502, 503, 504)
def is_stub_enabled(env_val: str) -> bool:
    return str(env_val).strip() == '1'
def validate_summary_sentence(summary: str) -> bool:
    return 5 <= len(summary.strip()) <= 300
