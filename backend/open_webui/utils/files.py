import asyncio
import os
import base64
import io
import mimetypes
import re
from pathlib import Path
from typing import Optional

import aiofiles
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    UploadFile,
)
from open_webui.env import (
    AIOHTTP_CLIENT_ALLOW_REDIRECTS,
    AIOHTTP_CLIENT_SESSION_SSL,
    ENABLE_IMAGE_CONTENT_TYPE_EXTENSION_FALLBACK,
)
from open_webui.models.chats import Chats
from open_webui.models.files import Files
from open_webui.retrieval.web.utils import get_ssrf_safe_session, validate_url
from open_webui.routers.files import upload_file_handler
from open_webui.utils.access_control.files import has_access_to_file
from open_webui.routers.images import (
    get_image_data,
    upload_image,
)
from open_webui.storage.provider import Storage

BASE64_IMAGE_URL_PREFIX = re.compile(r'data:image/\w+;base64,', re.IGNORECASE)
MARKDOWN_IMAGE_URL_PATTERN = re.compile(r'!\[(.*?)\]\((.+?)\)', re.IGNORECASE)

# Extension-based MIME fallback, only used when ENABLE_IMAGE_CONTENT_TYPE_EXTENSION_FALLBACK is True.
_IMAGE_MIME_FALLBACK = {
    '.webp': 'image/webp',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.gif': 'image/gif',
    '.svg': 'image/svg+xml',
    '.bmp': 'image/bmp',
    '.tiff': 'image/tiff',
    '.tif': 'image/tiff',
    '.ico': 'image/x-icon',
    '.heic': 'image/heic',
    '.heif': 'image/heif',
    '.avif': 'image/avif',
}


async def get_image_base64_from_url(url: str, user=None) -> Optional[str]:
    try:
        if url.startswith('http'):
            from open_webui.models.config import Config

            max_bytes = None
            try:
                max_size_mb = int(await Config.get('rag.file.max_size') or 0)
            except (TypeError, ValueError):
                max_size_mb = 0
            if max_size_mb > 0:
                max_bytes = max_size_mb * 1024 * 1024

            # Validate URL to prevent SSRF attacks against local/private networks.
            # allow_redirects=False prevents redirect-based SSRF: validate_url() is
            # called only on the originally-submitted URL; following 3xx redirects
            # without re-validation would let an attacker reach private IPs via a
            # public host that redirects internally (e.g. cloud-metadata exfil).
            await asyncio.to_thread(validate_url, url)
            # Fetch through an SSRF-safe session that re-checks the connect-time IP, so a
            # rebinding DNS answer that passed validate_url cannot reach an internal address.
            async with get_ssrf_safe_session() as session:
                async with session.get(
                    url, ssl=AIOHTTP_CLIENT_SESSION_SSL, allow_redirects=AIOHTTP_CLIENT_ALLOW_REDIRECTS
                ) as response:
                    response.raise_for_status()
                    image_data = bytearray()
                    total = 0
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        total += len(chunk)
                        if max_bytes is not None and total > max_bytes:
                            return None
                        image_data.extend(chunk)
                    encoded_string = base64.b64encode(image_data).decode('utf-8')
                    content_type = response.headers.get('Content-Type', 'image/png')
                    return f'data:{content_type};base64,{encoded_string}'
        else:
            # Non-URL string — treat as file_id. Delegate to the canonical
            # file-ID resolver which enforces ownership/access checks.
            return await get_image_base64_from_file_id(url, user=user)

    except Exception:
        return None


async def get_image_url_from_base64(request, base64_image_string, metadata, user):
    if BASE64_IMAGE_URL_PREFIX.match(base64_image_string):
        image_url = ''
        # Extract base64 image data from the line
        image_data, content_type = await get_image_data(base64_image_string)
        if image_data is not None:
            _, image_file = await upload_image(
                request,
                image_data,
                content_type,
                metadata,
                user,
            )
            image_url = image_file['url']

        return image_url
    return None


async def convert_markdown_base64_images(request, content: str, metadata, user):
    MIN_REPLACEMENT_URL_LENGTH = 1024
    result_parts = []
    last_end = 0

    for match in MARKDOWN_IMAGE_URL_PATTERN.finditer(content):
        result_parts.append(content[last_end : match.start()])
        base64_string = match.group(2)
        if len(base64_string) > MIN_REPLACEMENT_URL_LENGTH:
            url = await get_image_url_from_base64(request, base64_string, metadata, user)
            if url:
                result_parts.append(f'![{match.group(1)}]({url})')
            else:
                result_parts.append(match.group(0))
        else:
            result_parts.append(match.group(0))
        last_end = match.end()

    result_parts.append(content[last_end:])
    return ''.join(result_parts)


def load_b64_audio_data(b64_str):
    try:
        if ',' in b64_str:
            header, b64_data = b64_str.split(',', 1)
        else:
            b64_data = b64_str
            header = 'data:audio/wav;base64'
        audio_data = base64.b64decode(b64_data)
        content_type = header.split(';')[0].split(':')[1] if ';' in header else 'audio/wav'
        return audio_data, content_type
    except Exception as e:
        print(f'Error decoding base64 audio data: {e}')
        return None, None


async def upload_audio(request, audio_data, content_type, metadata, user):
    audio_format = mimetypes.guess_extension(content_type)
    file = UploadFile(
        file=io.BytesIO(audio_data),
        filename=f'generated-{audio_format}',  # will be converted to a unique ID on upload_file
        headers={
            'content-type': content_type,
        },
    )
    file_item = await upload_file_handler(
        request,
        file=file,
        metadata=metadata,
        process=False,
        user=user,
    )
    url = request.app.url_path_for('get_file_content_by_id', id=file_item.id)
    return url


async def get_audio_url_from_base64(request, base64_audio_string, metadata, user):
    if 'data:audio/wav;base64' in base64_audio_string:
        audio_url = ''
        # Extract base64 audio data from the line
        audio_data, content_type = load_b64_audio_data(base64_audio_string)
        if audio_data is not None:
            audio_url = await upload_audio(
                request,
                audio_data,
                content_type,
                metadata,
                user,
            )
        return audio_url
    return None


async def get_file_url_from_base64(request, base64_file_string, metadata, user):
    if BASE64_IMAGE_URL_PREFIX.match(base64_file_string):
        return await get_image_url_from_base64(request, base64_file_string, metadata, user)
    elif 'data:audio/wav;base64' in base64_file_string:
        return await get_audio_url_from_base64(request, base64_file_string, metadata, user)
    return None



# Match /api/v1/files/{id}/content with optional repeated /content and query/fragment.
_FILES_CONTENT_PATH_RE = re.compile(
    r'(?:(?:https?://[^/]+)?/api/v1/files/|/files/)(?P<id>[^/?#]+)/content(?:/content)*(?:[?#].*)?$',
    re.IGNORECASE,
)


def extract_file_id_from_media_url(url: str) -> Optional[str]:
    """Return a bare file id from relative or absolute Open WebUI file content URLs.

    Chat often sends `/api/v1/files/{id}/content` (sometimes doubled `/content/content`).
    Those must not be forwarded to Spark as video_url — resolve to the file id first.
    """
    if not url or not isinstance(url, str):
        return None
    value = url.strip()
    if not value or value.startswith('data:'):
        return None
    match = _FILES_CONTENT_PATH_RE.search(value)
    if match:
        return match.group('id')
    # Already a bare id (no slashes)
    if '/' not in value and '\\' not in value:
        return value
    return None


class VideoFileTooLargeError(Exception):
    """Raised when a video exceeds the hard base64-inline size cap."""

    def __init__(self, size_bytes: int, max_bytes: int, name: str = ''):
        self.size_bytes = size_bytes
        self.max_bytes = max_bytes
        self.name = name
        label = f' "{name}"' if name else ''
        super().__init__(
            f'Video{label} is too large to send to the model '
            f'({size_bytes} bytes; max {max_bytes} bytes). '
            f'Reduce the file size or raise VIDEO_BASE64_MAX_MB.'
        )


# Hard cap for inlining videos as data URLs (prevents OOM). Default 50 MiB.
try:
    _VIDEO_BASE64_MAX_MB = int(os.getenv('VIDEO_BASE64_MAX_MB', '50') or '50')
except (TypeError, ValueError):
    _VIDEO_BASE64_MAX_MB = 50
VIDEO_BASE64_MAX_BYTES = max(1, _VIDEO_BASE64_MAX_MB) * 1024 * 1024


async def get_video_base64_from_file_id(id: str, user=None) -> Optional[str]:
    """Resolve a stored file id to a data:video/...;base64 URL with a hard size cap."""
    file = await Files.get_file_by_id(id)
    if not file:
        return None

    if user is None:
        return None
    if file.user_id != user.id and user.role != 'admin' and not await has_access_to_file(file.id, 'read', user):
        return None

    try:
        file_path = await asyncio.to_thread(Storage.get_file, file.path)
        file_path = Path(file_path)
        if not file_path.is_file():
            return None

        size_bytes = file_path.stat().st_size
        if size_bytes > VIDEO_BASE64_MAX_BYTES:
            raise VideoFileTooLargeError(size_bytes, VIDEO_BASE64_MAX_BYTES, getattr(file, 'filename', '') or id)

        async with aiofiles.open(file_path, 'rb') as video_file:
            encoded_string = base64.b64encode(await video_file.read()).decode('utf-8')
        content_type = mimetypes.guess_type(file_path.name)[0] or (file.meta or {}).get('content_type') or 'video/mp4'
        if not str(content_type).startswith('video/'):
            content_type = 'video/mp4'
        return f'data:{content_type};base64,{encoded_string}'
    except VideoFileTooLargeError:
        raise
    except Exception:
        return None


async def get_video_base64_from_url(url: str, user=None) -> Optional[str]:
    """Resolve http(s) or file-id video_url values to data:video/...;base64."""
    try:
        if url.startswith('data:video/'):
            # Enforce size on already-inlined data URLs (base64 expands ~4/3).
            try:
                header, b64_data = url.split(',', 1)
            except ValueError:
                return None
            # Approximate decoded size without fully decoding twice.
            approx_bytes = (len(b64_data) * 3) // 4
            if approx_bytes > VIDEO_BASE64_MAX_BYTES:
                raise VideoFileTooLargeError(approx_bytes, VIDEO_BASE64_MAX_BYTES)
            return url

        # Relative/absolute OWUI file content paths → bare file id (never forward to Spark).
        file_id = extract_file_id_from_media_url(url)
        if file_id and (
            url.startswith('/')
            or '/api/v1/files/' in url
            or '/files/' in url
            or (file_id == url.strip())
        ):
            # Prefer file-id resolution for OWUI paths and bare ids.
            # Absolute http(s) that are NOT our files API still fall through below.
            if not url.startswith('http') or '/api/v1/files/' in url or '/files/' in url:
                return await get_video_base64_from_file_id(file_id, user=user)

        if url.startswith('http'):
            await asyncio.to_thread(validate_url, url)
            async with get_ssrf_safe_session() as session:
                async with session.get(
                    url, ssl=AIOHTTP_CLIENT_SESSION_SSL, allow_redirects=AIOHTTP_CLIENT_ALLOW_REDIRECTS
                ) as response:
                    response.raise_for_status()
                    video_data = bytearray()
                    total = 0
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        total += len(chunk)
                        if total > VIDEO_BASE64_MAX_BYTES:
                            raise VideoFileTooLargeError(total, VIDEO_BASE64_MAX_BYTES)
                        video_data.extend(chunk)
                    encoded_string = base64.b64encode(video_data).decode('utf-8')
                    content_type = response.headers.get('Content-Type', 'video/mp4')
                    if not str(content_type).startswith('video/'):
                        content_type = 'video/mp4'
                    return f'data:{content_type};base64,{encoded_string}'

        # Last resort: treat as bare file id
        if file_id:
            return await get_video_base64_from_file_id(file_id, user=user)
        return await get_video_base64_from_file_id(url, user=user)
    except VideoFileTooLargeError:
        raise
    except Exception:
        return None


async def get_image_base64_from_file_id(id: str, user=None) -> Optional[str]:
    file = await Files.get_file_by_id(id)
    if not file:
        return None

    # Gate file-by-id resolution by ownership to prevent exfiltration.
    # A caller could place another user's file_id in an image_url field;
    # without this check the server reads the file from disk, inlines it
    # base64 into the LLM request, and the content leaks via OCR/describe.
    # Owner, admin, and explicit read-grant holders are allowed.
    if user is None:
        return None
    if file.user_id != user.id and user.role != 'admin' and not await has_access_to_file(file.id, 'read', user):
        return None

    try:
        file_path = await asyncio.to_thread(Storage.get_file, file.path)
        file_path = Path(file_path)

        # Check if the file already exists in the cache
        if file_path.is_file():
            async with aiofiles.open(file_path, 'rb') as image_file:
                encoded_string = base64.b64encode(await image_file.read()).decode('utf-8')
            content_type = mimetypes.guess_type(file_path.name)[0] or (file.meta or {}).get('content_type')
            if not content_type and ENABLE_IMAGE_CONTENT_TYPE_EXTENSION_FALLBACK:
                content_type = _IMAGE_MIME_FALLBACK.get(file_path.suffix.lower())
            if not content_type:
                return None
            return f'data:{content_type};base64,{encoded_string}'
        else:
            return None
    except Exception:
        return None
