"""Do not render imported appointment URLs as executable browser schemes."""

from urllib.parse import urlsplit


def safe_video_link(value):
    if not isinstance(value, str):
        return ''
    try:
        parsed = urlsplit(value)
        return value if parsed.scheme == 'https' and parsed.netloc else ''
    except ValueError:
        return ''
