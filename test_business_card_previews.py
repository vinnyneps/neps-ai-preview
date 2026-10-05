import base64
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
import service


class BusinessCardPreviews(unittest.TestCase):
    def test_every_card_size_and_side_option(self):
        for product in service.PRODUCT_FINISHES:
            sides = service.SIDES if product == 'gloss-emboss-business-cards' else service.PRINTING_SIDES
            for size in service.SIZES:
                for side in sides:
                    with self.subTest(product=product, size=size, side=side):
                        payload = dict(product=product, size=size, sides=side, prompt='A clean business card for Example Company', token='test-token')
                        self.assertEqual(service.validate_input(payload), payload['prompt'])

    def test_unknown_product_and_wrong_side_options_rejected(self):
        payload = dict(product='unknown-card', size='3.5"x2"', sides='Single Sided', prompt='Example company business card', token='test-token')
        with self.assertRaises(ValueError):
            service.validate_input(payload)
        payload.update(product='gloss-emboss-business-cards')
        with self.assertRaises(ValueError):
            service.validate_input(payload)
        payload.update(product='enviro-cards', sides='Both Sides')
        with self.assertRaises(ValueError):
            service.validate_input(payload)

    def test_existing_gloss_request_without_product_still_works(self):
        payload = dict(size='3.5"x2"', sides='One Side', prompt='Example company business card', token='test-token')
        service.validate_input(payload)
        self.assertIn('Raised gloss: One Side', service.design_instruction(payload, payload['prompt']))

    def test_all_products_generate_and_save_correct_finish(self):
        output = io.BytesIO()
        Image.new('RGB', (20, 10), 'white').save(output, 'PNG')
        with tempfile.TemporaryDirectory() as directory, patch.object(service, 'DATA', Path(directory)), patch.object(service, 'PUBLIC', 'https://preview.example'), patch.dict(service.os.environ, {'OPENAI_API_KEY': 'test-only'}), patch.object(service, 'suggest_gloss_regions', return_value=[]), patch.object(service, 'call', return_value={'data': [{'b64_json': base64.b64encode(output.getvalue()).decode()}]}) as provider:
            for product, finish in service.PRODUCT_FINISHES.items():
                side = 'One Side' if product == 'gloss-emboss-business-cards' else 'Single Sided'
                payload = dict(product=product, size='3.5"x2"', sides=side)
                result = service.generate(payload, 'Example Company', None)
                sent = json.loads(provider.call_args.args[1])
                self.assertIn(finish, sent['prompt'])
                if product != 'gloss-emboss-business-cards':
                    self.assertIn('Show the reverse without lettering or logo artwork.', sent['prompt'])
                metadata = json.loads(next((Path(directory) / 'concepts').glob(result['concept_id'] + '*.json')).read_text())
                self.assertEqual(metadata['product'], product)
                self.assertEqual(metadata['sides'], side)

    def test_health_advertises_all_twelve_products(self):
        response = b''.join(service.application({'REQUEST_METHOD': 'GET', 'PATH_INFO': '/health'}, lambda *_: None))
        capabilities = json.loads(response)
        self.assertEqual(len(capabilities['supported_products']), 12)
        self.assertEqual(set(capabilities['supported_products']), set(service.PRODUCT_FINISHES))
        self.assertTrue(capabilities['original_logo_retained'])


if __name__ == '__main__':
    unittest.main()
