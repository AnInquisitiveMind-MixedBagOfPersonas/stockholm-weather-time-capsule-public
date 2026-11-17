from io import BytesIO
from pathlib import Path
import re
from urllib.parse import urlencode
from tempfile import TemporaryDirectory
from unittest import TestCase

from app import create_app
from dispatch import DispatchError


class DateExplorerTests(TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        file = Path(directory.name) / 'daily.csv'
        file.write_text(
            'date,raw,homo,adjust,site\n'
            '1756-01-01,0,0,0,1\n'
            '1756-05-15,-999.0,9.2,8.9,2\n'
            '1872-02-29,2.1,2.0,2.0,1\n'
            '1876-02-29,4.0,3.8,3.8,1\n'
            '1897-05-15,10.4,9.6,9.6,1\n'
            '1901-12-10,-4.6,-4.9,-4.9,1\n'
            '2000-02-29,2.5,2.0,2.0,1\n'
            '2024-02-29,2.3,1.9,1.9,3\n'
            '2025-12-10,7.0,6.4,6.4,3\n'
            '2026-05-15,10.8,9.4,9.4,3\n', encoding='utf-8',
        )
        self.app = create_app(file)
        self.fixture = file

    def request(self, query: str = '', path: str = '/', method: str = 'GET', data: bytes = b'', host: str = '127.0.0.1:8769') -> tuple[str, dict[str, str], str]:
        result: list[tuple[str, list[tuple[str, str]]]] = []

        def start(status: str, headers: list[tuple[str, str]]) -> None:
            result.append((status, headers))

        environ = {
            'QUERY_STRING': query, 'PATH_INFO': path, 'REQUEST_METHOD': method,
            'CONTENT_TYPE': 'application/x-www-form-urlencoded',
            'CONTENT_LENGTH': str(len(data)), 'wsgi.input': BytesIO(data), 'HTTP_HOST': host,
        }
        body = b''.join(self.app(environ, start))
        status, headers = result[0]
        return status, dict(headers), body.decode('utf-8')

    def test_featured_capsules_have_correct_history_and_comparison(self) -> None:
        for query, headline, latest, delta in (
            ('', 'The exhibition opens', '15 May 2026', '0.2 °C cooler'),
            ('date=1901-12-10', 'The first Nobel Prizes', '10 December 2025', '11.3 °C warmer'),
        ):
            with self.subTest(query=query):
                status, _, body = self.request(query)
                self.assertEqual(status, '200 OK')
                for expected in (headline, latest, delta, 'Oskar II'):
                    self.assertIn(expected, body)

    def test_custom_leap_day_uses_only_real_leap_days(self) -> None:
        status, _, body = self.request('date=2000-02-29')
        self.assertEqual(status, '200 OK')
        self.assertIn('29 February 2024', body)
        self.assertIn('2 readings', body)
        self.assertIn('No sourced event', body)
        self.assertNotIn('Oskar II', body)

    def test_invalid_dates_and_payloads_are_not_reflected(self) -> None:
        for query in ('date=2026-02-29', 'date=1755-01-01', 'date=2030-01-01',
                      'date=1897-05-15&date=1901-12-10', 'date=%3Cscript%3E', 'date=' + '0' * 200):
            with self.subTest(query=query):
                status, headers, body = self.request(query)
                self.assertEqual(status, '400 Bad Request')
                self.assertNotIn('<script>', body)
                self.assertIn("default-src 'none'", headers['Content-Security-Policy'])

    def test_missing_data_and_latest_date_have_clear_states(self) -> None:
        _, _, missing = self.request('date=1897-05-16')
        self.assertIn('no adjusted daily mean', missing)
        _, _, latest = self.request('date=2026-05-15')
        self.assertIn('No newer reading', latest)
        _, _, estimated = self.request('date=1756-05-15')
        self.assertIn('an estimate from Uppsala', estimated)
        self.assertIn('Unavailable', estimated)

    def test_only_expected_routes_and_methods(self) -> None:
        self.assertEqual(self.request(path='/healthz')[2], 'ok\n')
        self.assertEqual(self.request(path='/reader.css')[1]['Content-Type'], 'text/css; charset=utf-8')
        self.assertEqual(self.request(path='/stockholm_daily_mean_temperature.csv')[0], '404 Not Found')
        self.assertEqual(self.request(method='POST')[0], '405 Method Not Allowed')
        self.assertEqual(self.request(method='HEAD')[2], '')

    def test_dispatch_uses_selected_date_facts_and_escapes_model_text(self) -> None:
        calls: list[dict[str, str]] = []

        def fake(facts: dict[str, str]) -> str:
            calls.append(facts)
            return f'<script>alert(1)</script> The reading was {facts["adjusted daily mean"]}.'

        self.app = create_app(self.fixture, fake)
        _, headers, form = self.request()
        self.assertEqual(headers['Cache-Control'], 'no-store')
        token = re.search(r'name="token" value="([^"]+)"', form)
        self.assertIsNotNone(token)
        assert token
        for body in (b'date=1897-05-15&token=invalid', b'x' * 257,
                     urlencode({'date': '2000-02-30', 'token': token[1]}).encode(),
                     urlencode({'date': '1897-05-16', 'token': token[1]}).encode()):
            self.assertEqual(self.request(method='POST', data=body)[0], '400 Bad Request')
        self.assertEqual(self.request(host='unexpected.example')[0], '403 Forbidden')
        self.assertEqual(calls, [])
        body = urlencode({'date': '1897-05-15', 'token': token[1]}).encode()
        status, _, rendered = self.request(method='POST', data=body)
        self.assertEqual(status, '200 OK')
        self.assertIn('&lt;script&gt;', rendered)
        self.assertNotIn('<script>', rendered)
        self.assertEqual(calls[0]['adjusted daily mean'], '9.6 °C')
        self.assertEqual(calls[0]['latest compared with selected date'], '0.2 °C cooler')
        self.assertIn('Industrial Exhibition', calls[0]['verified historical event'])

        self.assertNotIn('Create time capsule', rendered)
        for _ in range(6):
            status, _, repeated = self.request(method='POST', data=body)
            self.assertEqual(status, '200 OK')
            self.assertIn('&lt;script&gt;', repeated)
        _, _, reopened = self.request('date=1897-05-15')
        self.assertIn('&lt;script&gt;', reopened)
        self.assertEqual(len(calls), 1)

        body = urlencode({'date': '2000-02-29', 'token': token[1]}).encode()
        status, _, rendered = self.request(method='POST', data=body)
        self.assertEqual(status, '200 OK')
        self.assertIn('No sourced event', rendered)
        self.assertEqual(calls[1]['date'], '29 February 2000')
        self.assertEqual(calls[1]['latest compared with selected date'], '0.1 °C cooler')
        self.assertNotIn('verified historical event', calls[1])
        self.assertNotIn('monarch', calls[1])

    def test_provider_failure_keeps_weather_visible(self) -> None:
        attempts = 0

        def generate(facts: dict[str, str]) -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise DispatchError('Provider unavailable.')
            return 'Stockholm recorded an adjusted daily mean of 9.6 °C.'

        self.app = create_app(self.fixture, generate)
        _, _, form = self.request()
        token = re.search(r'name="token" value="([^"]+)"', form)
        assert token
        body = urlencode({'date': '1897-05-15', 'token': token[1]}).encode()
        status, _, rendered = self.request(method='POST', data=body)
        self.assertEqual(status, '503 Service Unavailable')
        self.assertIn('Provider unavailable.', rendered)
        self.assertIn('9.6 °C', rendered)
        self.assertIn('Create time capsule', rendered)
        status, _, rendered = self.request(method='POST', data=body)
        self.assertEqual(status, '200 OK')
        self.assertIn('Stockholm recorded an adjusted daily mean', rendered)
        self.assertEqual(attempts, 2)
