"""Explicit text-to-image requests via the authenticated OpenRouter Image API.
No arbitrary image URLs are fetched; only bounded base64 raster output is accepted.
"""
import base64
import binascii
import json
import re
import httpx

DEFAULT_MODEL='black-forest-labs/flux.2-klein-4b'
TIMEOUT=180
MAX_IMAGE_BYTES=10*1024*1024
MAX_RESPONSE_BYTES=16*1024*1024
MAX_PROMPT_CHARS=2000

_INTENT=re.compile(
 r'^(?:гоша[,!:\s]+)?(?:пожалуйста[,!:\s]+)?(?:'
 r'(?:нарисуй|изобрази|проиллюстрируй)\b|'
 r'(?:создай|сделай|сгенерируй|генерируй)\s+(?:мне\s+)?(?:пожалуйста\s+)?(?:картин\w*|изображени\w*|рисунок|иллюстраци\w*|постер|баннер|обложк\w*|лого\w*|фото\w*|арт)\b|'
 r'(?:draw|paint|illustrate)\b|(?:create|generate|make)\s+(?:an?\s+)?(?:image|picture|illustration|poster|logo)\b)',re.I)


def is_image_request(text):
    return bool(_INTENT.match((text or '').strip()))


class ImageError(Exception):
    def __init__(self,kind='unavailable',status=None):
        super().__init__(kind);self.kind=kind;self.status=status


def decode_result(payload):
    try:
        item=payload['data'][0]
        encoded=item['b64_json']
        if not isinstance(encoded,str) or len(encoded)>MAX_IMAGE_BYTES*4//3+8:
            raise ImageError('invalid_image')
        raw=base64.b64decode(encoded,validate=True)
        if not raw or len(raw)>MAX_IMAGE_BYTES:raise ImageError('invalid_image')
        if raw.startswith(b'\xff\xd8\xff'):return raw,'gosha-image.jpg'
        if raw.startswith(b'\x89PNG\r\n\x1a\n'):return raw,'gosha-image.png'
        raise ImageError('invalid_image')
    except (KeyError,IndexError,TypeError,ValueError,binascii.Error) as exc:
        raise ImageError('invalid_image') from exc


async def create_image(prompt,key,model=DEFAULT_MODEL):
    if not key:raise ImageError('configuration')
    if not prompt or len(prompt)>MAX_PROMPT_CHARS:raise ImageError('prompt')
    payload={'model':model,'prompt':prompt,'n':1,'aspect_ratio':'1:1','output_format':'jpeg'}
    # No automatic retries or provider fallbacks: prevent unexpected repeated paid calls.
    payload['provider']={'allow_fallbacks':False}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT,connect=15),follow_redirects=False) as session:
            async with session.stream('POST','https://openrouter.ai/api/v1/images',
                                      headers={'Authorization':'Bearer '+key},json=payload) as response:
                if response.status_code!=200:
                    status=response.status_code
                    kind='configuration' if status in (401,402,403,404) else ('refused' if status in (400,422) else 'unavailable')
                    raise ImageError(kind,status)
                buf=bytearray()
                async for chunk in response.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf)>MAX_RESPONSE_BYTES:raise ImageError('invalid_image')
        data=json.loads(buf)
        if not isinstance(data,dict):raise ImageError('invalid_image')
        return decode_result(data)
    except (httpx.HTTPError,TimeoutError) as exc:
        raise ImageError('unavailable') from exc
    except (ValueError,UnicodeError) as exc:
        raise ImageError('invalid_image') from exc
