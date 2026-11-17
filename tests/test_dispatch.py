import json
from datetime import date
from unittest import TestCase
from unittest.mock import patch

from boto3.session import Session
from botocore.exceptions import ReadTimeoutError
from botocore.stub import Stubber

from app import ROOT, WeatherArchive, dispatch_facts
from dispatch import BEDROCK_REGION, INSTRUCTIONS, BedrockDispatch, DispatchError


class BedrockDispatchTests(TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.archive = WeatherArchive(ROOT / 'stockholm_daily_mean_temperature.csv')

    def setUp(self) -> None:
        # The SDK sees inert credentials and Stubber intercepts the request before HTTP.
        client = Session(aws_access_key_id='test', aws_secret_access_key='test').client(
            'bedrock-runtime', region_name=BEDROCK_REGION,
        )
        self.stub = Stubber(client)
        self.stub.activate()
        self.addCleanup(self.stub.deactivate)
        self.generator = BedrockDispatch(client)

    def expect(self, facts: dict[str, str]) -> dict[str, object]:
        return {
            'modelId': 'amazon.nova-lite-v1:0',
            'system': [{'text': INSTRUCTIONS}],
            'messages': [{'role': 'user', 'content': [{'text': json.dumps(facts, ensure_ascii=False)}]}],
            'inferenceConfig': {'maxTokens': 200, 'temperature': 0.2},
        }

    def respond(self, facts: dict[str, str], text: str, stop: str = 'end_turn') -> None:
        self.stub.add_response(
            'converse', {
                'output': {'message': {'role': 'assistant', 'content': [{'text': text}]}},
                'stopReason': stop,
                'usage': {'inputTokens': 100, 'outputTokens': 50, 'totalTokens': 150},
                'metrics': {'latencyMs': 20},
            },
            self.expect(facts),
        )

    def test_converse_receives_published_weather_and_only_curated_history(self) -> None:
        exhibition = dispatch_facts(date(1897, 5, 15), self.archive)
        self.assertEqual(exhibition['adjusted daily mean'], '9.6 °C')
        self.assertEqual(exhibition['latest adjusted daily mean'], '9.4 °C')
        self.assertEqual(exhibition['latest compared with selected date'], '0.2 °C cooler')
        self.assertIn('Djurgården', exhibition['verified historical event'])
        leap_day = dispatch_facts(date(2000, 2, 29), self.archive)
        self.assertEqual(leap_day['adjusted daily mean'], '3.8 °C')
        self.assertEqual(leap_day['latest same calendar date'], '29 February 2024')
        self.assertEqual(leap_day['latest compared with selected date'], '1.4 °C cooler')
        self.assertNotIn('verified historical event', leap_day)
        self.assertNotIn('monarch', leap_day)

        for facts, text in (
            (exhibition, 'Stockholm averaged 9.6 °C on 15 May 1897, when the exhibition opened.'),
            (leap_day, 'Stockholm averaged 3.8 °C on 29 February 2000.'),
        ):
            self.respond(facts, text)
            self.assertEqual(self.generator(facts), text)
        self.stub.assert_no_pending_responses()

    def test_qwen_uses_selected_model_and_displays_only_dispatch_text(self) -> None:
        facts = {'date': '15 May 1897'}
        text = 'Stockholm averaged 9.6 °C on 15 May 1897.'
        self.stub.add_response(
            'converse', {
                'output': {'message': {'role': 'assistant', 'content': [
                    {'reasoningContent': {'reasoningText': {'text': 'Example reasoning'}}},
                    {'text': text},
                ]}},
                'stopReason': 'end_turn',
                'usage': {'inputTokens': 100, 'outputTokens': 50, 'totalTokens': 150},
                'metrics': {'latencyMs': 20},
            },
            dict(self.expect(facts), modelId='qwen.qwen3-235b-a22b-2507-v1:0'),
        )
        with patch.dict('os.environ', {'CAPSULE_BEDROCK_MODEL': 'qwen'}):
            generator = BedrockDispatch.from_environment()
        with patch('boto3.client', return_value=self.generator._client):
            self.assertEqual(generator(facts), text)
        self.stub.assert_no_pending_responses()

    def test_unknown_model_setting_is_rejected(self) -> None:
        with patch.dict('os.environ', {'CAPSULE_BEDROCK_MODEL': 'other'}):
            with self.assertRaisesRegex(ValueError, 'must be nova-lite or qwen'):
                BedrockDispatch.from_environment()

    def test_access_denial_does_not_reveal_service_details(self) -> None:
        facts = {'date': '15 May 1897'}
        self.stub.add_client_error(
            'converse', 'AccessDeniedException', 'Private diagnostic', 403,
            expected_params=self.expect(facts),
        )
        with self.assertRaisesRegex(DispatchError, 'Bedrock declined access') as error:
            self.generator(facts)
        self.assertNotIn('Private diagnostic', str(error.exception))
        self.stub.assert_no_pending_responses()

    def test_read_timeout_is_handled(self) -> None:
        with patch.object(self.generator._client, 'converse', side_effect=ReadTimeoutError(endpoint_url='test')):
            with self.assertRaisesRegex(DispatchError, 'Bedrock could not be reached'):
                self.generator({})

    def test_rejects_truncated_and_malformed_output(self) -> None:
        facts = {'date': '15 May 1897'}
        self.respond(facts, 'Truncated', stop='max_tokens')
        self.respond(facts, '   ')
        for _ in range(2):
            with self.assertRaisesRegex(DispatchError, 'complete short dispatch'):
                self.generator(facts)
        with patch.object(self.generator._client, 'converse', return_value={'output': None}):
            with self.assertRaisesRegex(DispatchError, 'complete short dispatch'):
                self.generator(facts)
        self.stub.assert_no_pending_responses()

    def test_preview_limits_both_settings_and_recovers_after_an_hour(self) -> None:
        facts = {'date': '15 May 1897'}
        with patch.dict('os.environ', {'CAPSULE_BEDROCK_MODEL': 'nova-lite', 'CAPSULE_DISPATCH_LIMIT': '10'}):
            demo = BedrockDispatch.from_environment()

        for limit, generator in ((5, self.generator), (10, demo)):
            with self.subTest(limit=limit):
                for _ in range(limit + 1):
                    self.respond(facts, 'A short dispatch.')
                with (
                    patch('boto3.client', return_value=self.generator._client),
                    patch('dispatch.monotonic', side_effect=[0] * (limit + 1) + [3600]),
                ):
                    for _ in range(limit):
                        generator(facts)
                    with self.assertRaisesRegex(DispatchError, f'{limit} generation attempts per hour'):
                        generator(facts)
                    generator(facts)
                self.stub.assert_no_pending_responses()

    def test_invalid_dispatch_limit_setting_is_rejected(self) -> None:
        with patch.dict('os.environ', {'CAPSULE_DISPATCH_LIMIT': '0'}):
            with self.assertRaisesRegex(ValueError, 'must be 5 or 10'):
                BedrockDispatch.from_environment()
