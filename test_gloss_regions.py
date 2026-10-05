import base64
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
import service


def region(side='front', category='Logo'):
    return {'category': category, 'side': side, 'points': [{'x': 100, 'y': 100}, {'x': 400, 'y': 100}, {'x': 400, 'y': 250}, {'x': 100, 'y': 250}]}


class GlossRegions(unittest.TestCase):
    def test_side_limits_and_invalid_coordinates(self):
        for side in ['One Side', 'Single Sided']:
            self.assertEqual(service.validate_gloss_regions([region(), region('back')], {'sides': side}), [region()])
        self.assertEqual(len(service.validate_gloss_regions([region(), region('back')], {'sides': 'Both Sides'})), 2)
        for bad in [float('nan'), float('inf'), -1, 1001, True, '100']:
            item = region(); item['points'][0]['x'] = bad
            self.assertEqual(service.validate_gloss_regions([item], {'sides': 'Both Sides'}), [])
        self.assertEqual(service.validate_gloss_regions([region(category='Photo')], {'sides': 'Both Sides'}), [])
        full = region(); full['points'] = [{'x': 0, 'y': 0}, {'x': 1000, 'y': 0}, {'x': 1000, 'y': 1000}, {'x': 0, 'y': 1000}]
        self.assertEqual(service.validate_gloss_regions([full], {'sides': 'Both Sides'}), [])

    def image_bytes(self):
        stream = io.BytesIO(); Image.new('RGB', (200, 100), 'white').save(stream, 'PNG'); return stream.getvalue()

    def test_vision_request_schema_and_coordinate_response(self):
        response = {'choices': [{'message': {'content': json.dumps({'regions': [region()]})}}]}
        with patch.dict(service.os.environ, {'OPENAI_API_KEY': 'test-only'}), patch.object(service, 'call', return_value=response) as provider:
            self.assertEqual(service.suggest_gloss_regions(self.image_bytes(), {'product': 'spot-uv-cards', 'sides': 'Single Sided'}), [region()])
            request = json.loads(provider.call_args.args[1])
            self.assertEqual(request['response_format']['type'], 'json_schema')
            self.assertFalse(request['store'])
            self.assertEqual(provider.call_args.kwargs['timeout'], 25)
            self.assertTrue(request['messages'][1]['content'][0]['image_url']['url'].startswith('data:image/jpeg;base64,'))

    def test_generation_integration_and_graceful_overlay_failure(self):
        raw = self.image_bytes()
        with tempfile.TemporaryDirectory() as directory, patch.object(service, 'DATA', Path(directory)), patch.object(service, 'PUBLIC', 'https://preview.example'), patch.dict(service.os.environ, {'OPENAI_API_KEY': 'test-only'}), patch.object(service, 'call', return_value={'data': [{'b64_json': base64.b64encode(raw).decode()}]}), patch.object(service, 'suggest_gloss_regions', return_value=[region()]) as guide:
            for product in ['gloss-emboss-business-cards', 'spot-uv-cards', 'silk-laminated-cards']:
                payload = {'product': product, 'size': '3.5"x2"', 'sides': 'One Side' if product == 'gloss-emboss-business-cards' else 'Single Sided'}
                result = service.generate(payload, 'Example Company', None)
                expected = [region()] if product in service.GLOSS_PRODUCTS else []
                self.assertEqual(result['suggested_gloss_regions'], expected)
                saved = json.loads(next((Path(directory) / 'concepts').glob(result['concept_id'] + '*.json')).read_text())
                self.assertEqual(saved['suggested_gloss_regions'], expected)
            self.assertEqual(guide.call_count, 2)
            guide.side_effect = TimeoutError()
            result = service.generate({'product': 'spot-uv-cards', 'size': '3.5"x2"', 'sides': 'Single Sided'}, 'Example Company', None)
            self.assertEqual(result['suggested_gloss_regions'], [])
            self.assertTrue(next((Path(directory) / 'concepts').glob(result['concept_id'] + '*.png')).is_file())


if __name__ == '__main__':
    unittest.main()
