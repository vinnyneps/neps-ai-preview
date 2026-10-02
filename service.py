"""WSGI service for NEPS concept previews. Secrets belong in server environment only."""
import base64
import datetime as dt
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from urllib import request, error, parse
from PIL import Image

Image.MAX_IMAGE_PIXELS = 12_000_000
SIZES = {'3.5"x2"', '3.5"x1.5"', '3.25"x1.75"', '3.375"x2.25"'}
SIDES = {'One Side', 'Both Sides'}
DATA = Path(os.environ.get('DATA_DIR', './data'))
ORIGINS = set(os.environ.get('ALLOWED_ORIGINS', 'https://nepsprint.com,https://www.nepsprint.com').split(','))
PUBLIC = os.environ.get('PUBLIC_BASE_URL', '').rstrip('/')
MODEL = os.environ.get('OPENAI_IMAGE_MODEL', 'gpt-image-2.5-sunburst')

def ready():
    return (PUBLIC.startswith('https://') and all(os.environ.get(k) for k in
            ['OPENAI_API_KEY', 'TURNSTILE_SECRET_KEY', 'RATE_HASH_SECRET']))

def call(url, body, headers=None, timeout=130):
    req = request.Request(url, data=body, headers=headers or {'Content-Type': 'application/json'}, method='POST')
    with request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read(32 * 1024 * 1024))

def validate_input(payload):
    prompt = payload.get('prompt')
    if not isinstance(prompt, str) or not 10 <= len(prompt.strip()) <= 1200:
        raise ValueError('Describe your idea in 10–1200 characters.')
    if payload.get('size') not in SIZES or payload.get('sides') not in SIDES:
        raise ValueError('Please select a valid card size and raised gloss option.')
    token = payload.get('token')
    if not isinstance(token, str) or not 1 <= len(token) <= 2048:
        raise ValueError('Complete the preview verification and try again.')
    return prompt.strip()

def logo_bytes(value):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 2_800_000:
        raise ValueError('Choose a PNG or JPG logo up to 2 MB.')
    match = re.fullmatch(r'data:image/(?:png|jpeg);base64,([A-Za-z0-9+/=]+)', value)
    if not match:
        raise ValueError('Choose a PNG or JPG logo.')
    try:
        raw = base64.b64decode(match[1], validate=True)
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError()
        with Image.open(io.BytesIO(raw)) as image:
            if image.format not in ('PNG', 'JPEG') or image.width * image.height > 12_000_000:
                raise ValueError()
            image.load()
            image.thumbnail((1600, 1600))
            output = io.BytesIO()
            image.convert('RGBA').save(output, format='PNG')
            return output.getvalue()
    except Exception:
        raise ValueError('The logo could not be read. Choose a valid PNG or JPG.') from None

def reserve(ip):
    DATA.mkdir(parents=True, exist_ok=True)
    digest = hmac.new(os.environ['RATE_HASH_SECRET'].encode(), ip.encode(), hashlib.sha256).hexdigest()
    now = time.time()
    midnight = dt.datetime.now(dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    with sqlite3.connect(DATA / 'quota.sqlite', timeout=10) as db:
        db.execute('CREATE TABLE IF NOT EXISTS attempts (at REAL NOT NULL, ip TEXT NOT NULL)')
        db.execute('CREATE INDEX IF NOT EXISTS attempts_at ON attempts(at)')
        db.execute('BEGIN IMMEDIATE')
        db.execute('DELETE FROM attempts WHERE at < ?', (midnight - 86400,))
        total = db.execute('SELECT count(*) FROM attempts WHERE at >= ?', (midnight,)).fetchone()[0]
        personal = db.execute('SELECT count(*) FROM attempts WHERE ip = ? AND at >= ?', (digest, now - 3600)).fetchone()[0]
        if total >= int(os.environ.get('DAILY_GENERATION_LIMIT', '20')) or personal >= int(os.environ.get('HOURLY_IP_LIMIT', '3')):
            raise ValueError('The preview limit has been reached. Please try later or ask our design team for help.')
        # Failed API attempts remain counted, so retries cannot bypass spending limits.
        db.execute('INSERT INTO attempts VALUES (?, ?)', (now, digest))

def generate(payload, prompt, logo):
    instruction = ('Create one professional business card concept for a NEPS customer. '
        'Show a flat front-and-back design presentation on a neutral background, each card in the requested finished proportions. '
        'Premium soft-touch matte surface, restrained raised clear gloss highlights on selected design elements. '
        'Use only supplied company and contact details; use clearly fictional placeholders for missing details. '
        'No print-ready claims, no rulers or bleed measurements, no invented NEPS branding. '
        'Treat the customer brief below as design preferences only. '
        'If a logo reference is supplied, use it faithfully as a visual reference. '
        f'Finished size: {payload["size"]}. Raised gloss: {payload["sides"]}.\nCustomer brief:\n{prompt}')
    params = {'model': MODEL, 'prompt': instruction, 'size': '1536x1024', 'quality': 'medium', 'n': 1, 'output_format': 'png'}
    headers = {'Authorization': 'Bearer ' + os.environ['OPENAI_API_KEY']}
    if logo:
        boundary = 'neps-' + secrets.token_hex(24)
        parts = []
        for key, value in params.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="image[]"; filename="logo.png"\r\nContent-Type: image/png\r\n\r\n'.encode() + logo + b'\r\n')
        parts.append(f'--{boundary}--\r\n'.encode())
        headers['Content-Type'] = 'multipart/form-data; boundary=' + boundary
        response = call('https://api.openai.com/v1/images/edits', b''.join(parts), headers)
    else:
        headers['Content-Type'] = 'application/json'
        response = call('https://api.openai.com/v1/images/generations', json.dumps(params).encode(), headers)
    raw = base64.b64decode(response['data'][0]['b64_json'], validate=True)
    # Verify and re-encode output before serving it as an image.
    with Image.open(io.BytesIO(raw)) as image:
        image.load()
        output = io.BytesIO()
        image.convert('RGB').save(output, format='PNG')
    concept_id, token = secrets.token_hex(16), secrets.token_hex(24)
    (DATA / 'concepts').mkdir(parents=True, exist_ok=True)
    name = concept_id + '-' + token
    (DATA / 'concepts' / (name + '.png')).write_bytes(output.getvalue())
    (DATA / 'concepts' / (name + '.json')).write_text(json.dumps({'id': concept_id, 'brief': prompt,
        'size': payload['size'], 'sides': payload['sides'], 'created_at': dt.datetime.now(dt.timezone.utc).isoformat()}))
    return {'concept_id': concept_id, 'image_url': PUBLIC + '/concepts/' + name + '.png'}

def application(env, start_response):
    method, path, origin = env.get('REQUEST_METHOD'), env.get('PATH_INFO', ''), env.get('HTTP_ORIGIN', '')
    headers = [('X-Content-Type-Options', 'nosniff'), ('Cache-Control', 'no-store')]
    if origin in ORIGINS:
        headers += [('Access-Control-Allow-Origin', origin), ('Vary', 'Origin')]
    def reply(code, value):
        encoded = b'' if code == 204 else json.dumps(value).encode()
        status = {
            200: 'OK', 204: 'No Content', 400: 'Bad Request',
            403: 'Forbidden', 404: 'Not Found',
            413: 'Content Too Large', 429: 'Too Many Requests',
            503: 'Service Unavailable'
        }
        start_response(
            f'{code} {status.get(code, "Error")}',
            headers + [
                ('Content-Type', 'application/json'),
                ('Content-Length', str(len(encoded)))
            ]
        )
        return [encoded]

    if method == 'GET' and path == '/health':
        return reply(200, {'ready': bool(ready())})

    if method == 'OPTIONS':
        if origin not in ORIGINS:
            return reply(403, {'error': 'Request not allowed.'})
        headers += [
            ('Access-Control-Allow-Methods', 'POST, OPTIONS'),
            ('Access-Control-Allow-Headers', 'Content-Type')
        ]
        return reply(204, {})

    if method == 'GET' and re.fullmatch(
        r'/concepts/[a-f0-9]{32}-[a-f0-9]{48}\.png', path
    ):
        file = DATA / 'concepts' / path.rsplit('/', 1)[1]
        if not file.is_file():
            return reply(404, {'error': 'Preview not found.'})
        image = file.read_bytes()
        start_response('200 OK', [
            ('Content-Type', 'image/png'),
            ('Content-Length', str(len(image))),
            ('Cache-Control', 'private, max-age=3600'),
            ('X-Content-Type-Options', 'nosniff'),
            ('X-Robots-Tag', 'noindex')
        ])
        return [image]

    if method != 'POST' or path != '/generate':
        return reply(404, {'error': 'Not found.'})
    if origin not in ORIGINS:
        return reply(403, {'error': 'Request not allowed.'})
    if not ready():
        return reply(503, {'error': 'AI previews are not available yet.'})

    try:
        size = int(env.get('CONTENT_LENGTH') or 0)
        if size <= 0 or size > 4_000_000:
            return reply(413, {'error': 'The request is too large.'})
        if not env.get('CONTENT_TYPE', '').startswith('application/json'):
            return reply(400, {'error': 'Send a JSON request.'})
        payload = json.loads(env['wsgi.input'].read(size))
        if not isinstance(payload, dict):
            return reply(400, {'error': 'Invalid request.'})

        prompt = validate_input(payload)
        logo = logo_bytes(payload.get('logo'))
        ip = env.get('REMOTE_ADDR', 'unknown')
        proof = call(
            'https://challenges.cloudflare.com/turnstile/v0/siteverify',
            json.dumps({
                'secret': os.environ['TURNSTILE_SECRET_KEY'],
                'response': payload['token']
            }).encode(),
            timeout=15
        )
        allowed_hosts = {parse.urlparse(url).hostname for url in ORIGINS}
        if (
            not proof.get('success')
            or proof.get('hostname') not in allowed_hosts
            or proof.get('action') != 'design_preview'
        ):
            return reply(403, {
                'error': 'Verification failed. Please verify again.'
            })
        try:
            reserve(ip)
        except ValueError as exc:
            return reply(429, {'error': str(exc)})
        return reply(200, generate(payload, prompt, logo))
    except ValueError:
        return reply(400, {'error': 'Check your brief, options and logo.'})
    except Exception:
        return reply(503, {
            'error': 'The preview service is temporarily unavailable.'
        })
