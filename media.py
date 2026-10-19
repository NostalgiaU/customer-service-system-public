"""Bounded image transfers between known WeChat media hosts and configured Weiyu storage."""
from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx


class MediaError(RuntimeError):
    pass


@dataclass
class ImageFile:
    data: bytes
    mime: str
    filename: str


def image_file(data: bytes) -> ImageFile:
    # Accept raster formats only, never SVG/HTML disguised by a server MIME header.
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        mime, ext = 'image/png', 'png'
    elif data.startswith(b'\xff\xd8\xff'):
        mime, ext = 'image/jpeg', 'jpg'
    elif data.startswith((b'GIF87a', b'GIF89a')):
        mime, ext = 'image/gif', 'gif'
    elif data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        mime, ext = 'image/webp', 'webp'
    else:
        raise MediaError('图片格式不受支持或文件不是图片')
    return ImageFile(data, mime, 'image.' + ext)


def _validate_url(url: str, base: str, from_wechat: bool) -> None:
    parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.fragment:
        raise MediaError('图片地址不受支持')
    if from_wechat:
        extra = {h.strip().lower() for h in os.getenv('WX_MEDIA_HOSTS', '').split(',') if h.strip()}
        host = (parsed.hostname or '').lower()
        allowed = host == 'channels.weixin.qq.com' or host == 'mmecimage.cn' or host.endswith('.qpic.cn') or host in extra
        if parsed.scheme != 'https' or parsed.port not in (None, 443) or not allowed:
            raise MediaError('微信图片来源不在允许范围')
    else:
        origins = [base, *os.getenv('WEIYU_MEDIA_ORIGINS', '').split(',')]
        origin = (parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80))
        allowed = False
        for value in origins:
            if not value.strip():
                continue
            item = urlsplit(value.strip())
            if origin == (item.scheme, item.hostname, item.port or (443 if item.scheme == 'https' else 80)):
                allowed = True
        if parsed.scheme not in ('http', 'https') or not allowed or not parsed.path.startswith('/file/'):
            raise MediaError('微语图片地址不在允许的文件服务范围')


def download_image(url: str, base: str = '', *, from_wechat: bool = False) -> ImageFile:
    """No auth headers on downloads; validate every redirect and bound streamed bytes."""
    try:
        url = urljoin(base.rstrip('/') + '/', url) if base else url
        maximum = min(int(os.getenv('IMAGE_MAX_BYTES', str(30 * 1024 * 1024))), 30 * 1024 * 1024)
        if maximum <= 0:
            raise MediaError('图片大小限制配置无效')
        with httpx.Client(timeout=20, follow_redirects=False) as client:
            for _ in range(4):
                _validate_url(url, base, from_wechat)
                with client.stream('GET', url) as response:
                    if response.is_redirect:
                        url = urljoin(url, response.headers.get('location', ''))
                        continue
                    response.raise_for_status()
                    if int(response.headers.get('content-length', '0')) > maximum:
                        raise MediaError('图片超过允许大小')
                    data = bytearray()
                    for part in response.iter_bytes():
                        data.extend(part)
                        if len(data) > maximum:
                            raise MediaError('图片超过允许大小')
                    return image_file(bytes(data))
    except httpx.HTTPError as error:
        # Do not propagate signed media URLs or tokens through errors/logs.
        raise MediaError('图片下载失败（' + type(error).__name__ + '）') from None
    except ValueError:
        raise MediaError('图片地址、大小或响应格式无效') from None
    raise MediaError('图片重定向过多')
