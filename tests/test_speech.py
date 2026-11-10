import io
import json
import unittest
from unittest.mock import patch

from coach.speech import synthesize
from web import Handler


class SpeechTests(unittest.TestCase):
    def request(self, path, payload=None):
        handler = Handler.__new__(Handler)
        handler.path = path
        body = json.dumps(payload).encode() if payload is not None else b''
        handler.headers = {'Content-Length': str(len(body))}
        handler.rfile, handler.wfile = io.BytesIO(body), io.BytesIO()
        statuses, headers = [], {}
        handler.send_response = statuses.append
        handler.send_header = lambda name, value: headers.update({name: value})
        handler.end_headers = lambda: None
        handler.send_error = statuses.append
        if payload is None:
            handler.do_GET()
        else:
            handler.do_POST()
        return statuses, headers, handler.wfile.getvalue()

    def test_image_route(self):
        statuses, headers, body = self.request('/static/coach-explaining.png')
        self.assertEqual(statuses, [200])
        self.assertEqual(headers['Content-Type'], 'image/png')
        self.assertTrue(body.startswith(b'\x89PNG'))
        self.assertEqual(self.request('/static/../.env')[0], [404])

    @patch('web.synthesize', return_value=b'mp3-data')
    def test_audio_route(self, speech):
        statuses, headers, body = self.request('/api/speech', {'text': 'Olá'})
        self.assertEqual(statuses, [200])
        self.assertEqual(headers['Content-Type'], 'audio/mpeg')
        self.assertEqual(body, b'mp3-data')
        speech.assert_called_once_with({'text': 'Olá'})

    def test_invalid_input(self):
        for payload in ({}, {'text': ''}, {'text': 'a' * 20001}, {'text': 'Olá', 'voice': []}):
            with self.assertRaises(ValueError):
                synthesize(payload)
        self.assertEqual(self.request('/api/speech', {'text': ''})[0], [400])

    @patch('web.synthesize', side_effect=RuntimeError('Serviço indisponível'))
    def test_service_failure(self, speech):
        statuses, headers, body = self.request('/api/speech', {'text': 'Olá'})
        self.assertEqual(statuses, [503])
        self.assertEqual(json.loads(body)['error'], 'Serviço indisponível')


if __name__ == '__main__':
    unittest.main()
