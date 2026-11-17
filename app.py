"""Stockholm Weather Time Capsule, a small WSGI application."""

import csv
import os
import re
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from html import escape
from pathlib import Path
from string import Template
from threading import Lock
from typing import BinaryIO, Callable, Iterable, Mapping, cast
from urllib.parse import parse_qs
from secrets import compare_digest, token_urlsafe

from dispatch import BedrockDispatch, DispatchError, MistralDispatch


ROOT = Path(__file__).resolve().parent
DATASET = 'https://doi.org/10.17043/stockholm-historical-daily-temperature-2'
ROYAL_ARCHIVE = 'https://www.kungahuset.se/english/royal-house/the-bernadotte-dynasty/oskar-ii'
EXHIBITION = date(1897, 5, 15)
NOBEL = date(1901, 12, 10)
BASELINE_START, BASELINE_END = 1871, 1900
MAX_CACHED_DISPATCHES = 64
DATE_SHAPE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}')
MISSING = {'', '-999', '-999.0'}
MONTHS = ('January', 'February', 'March', 'April', 'May', 'June', 'July', 'August',
          'September', 'October', 'November', 'December')


@dataclass(frozen=True)
class Reading:
    adjusted: Decimal | None
    non_homogenised: Decimal | None
    site: str


@dataclass(frozen=True)
class Event:
    title: str
    summary: str
    archive_label: str
    archive_url: str


EVENTS = {
    EXHIBITION: Event('The exhibition opens',
        'The General Art and Industrial Exhibition opens on Djurgården during Oskar II’s reign.',
        'Watch archival film', 'https://stockholmskallan.stockholm.se/post/28971'),
    NOBEL: Event('The first Nobel Prizes',
        'Four of the inaugural Nobel Prizes are awarded in Stockholm. The Peace Prize is awarded in Christiania, now Oslo.',
        'Read an eyewitness account', 'https://www.nobelprize.org/ceremony/from-the-first-nobel-prize-award-ceremony-1901/'),
}


def parse_temperature(value: str) -> Decimal | None:
    if value in MISSING:
        return None
    number = Decimal(value)
    if not number.is_finite() or not -90 <= number <= 60:
        raise ValueError('Invalid temperature in dataset')
    return number


class WeatherArchive:
    def __init__(self, csv_path: Path) -> None:
        self.readings: dict[date, Reading] = {}
        self.calendar_dates: dict[tuple[int, int], list[date]] = {}
        with csv_path.open(encoding='utf-8-sig', newline='') as stream:
            reader = csv.DictReader(stream)
            if not {'date', 'raw', 'adjust', 'site'}.issubset(reader.fieldnames or ()):
                raise ValueError('Unrecognised daily temperature file')
            previous: date | None = None
            for row in reader:
                day = date.fromisoformat(row['date'])
                if previous is not None and day <= previous:
                    raise ValueError('Daily readings must be unique and in chronological order')
                if row['site'] not in {'1', '2', '3'}:
                    raise ValueError('Unrecognised observation source')
                adjusted = parse_temperature(row['adjust'])
                raw = parse_temperature(row['raw'])
                self.readings[day] = Reading(adjusted, raw, row['site'])
                if adjusted is not None:
                    self.calendar_dates.setdefault((day.month, day.day), []).append(day)
                previous = day
        if not self.readings:
            raise ValueError('The daily dataset is empty')
        self.first = min(self.readings)
        self.last = max(self.readings)

    def latest_match(self, selected: date) -> date | None:
        dates = self.calendar_dates.get((selected.month, selected.day), [])
        return dates[-1] if dates and dates[-1] > selected else None

    def baseline(self, selected: date) -> tuple[Decimal, int] | None:
        dates = self.calendar_dates.get((selected.month, selected.day), [])
        temperatures = [self.readings[day].adjusted for day in dates
                        if BASELINE_START <= day.year <= BASELINE_END]
        usable = [value for value in temperatures if value is not None]
        return (sum(usable, Decimal(0)) / len(usable), len(usable)) if usable else None


def readable_date(day: date) -> str:
    return f'{day.day} {MONTHS[day.month - 1]} {day.year}'


def degrees(value: Decimal | None) -> str:
    return 'Unavailable' if value is None else f'{value:.1f} °C'.replace('-', '−')


def direction(difference: Decimal) -> str:
    rounded = difference.quantize(Decimal('0.1'))
    if rounded == 0:
        return 'Same temperature to the nearest 0.1 °C'
    return f'{degrees(abs(rounded))} {"warmer" if rounded > 0 else "cooler"}'


def parse_date(query: str, archive: WeatherArchive) -> tuple[date, str]:
    if query == '':
        return EXHIBITION, ''
    if len(query) > 96:
        return EXHIBITION, 'Choose one date using the date picker.'
    try:
        fields = parse_qs(query, keep_blank_values=True, strict_parsing=True, max_num_fields=2)
        values = fields.get('date', [])
        if set(fields) != {'date'} or len(values) != 1 or not DATE_SHAPE.fullmatch(values[0]):
            raise ValueError
        chosen = date.fromisoformat(values[0])
    except ValueError:
        return EXHIBITION, 'Enter a valid date.'
    if not archive.first <= chosen <= archive.last:
        return EXHIBITION, f'Choose a date from {readable_date(archive.first)} to {readable_date(archive.last)}.'
    return chosen, ''


def render_history(day: date) -> tuple[str, str]:
    event = EVENTS.get(day)
    if event is None:
        return (f'Stockholm on {readable_date(day)}',
                '<p class="lede">No sourced event has been added for this date. Explore the observatory record alongside.</p>')
    summary = escape(event.summary)
    return event.title, (
        f'<p class="lede">{summary}</p>'
        f'<p class="regent"><span class="kicker">On the throne</span> <strong>Oskar II</strong> '
        f'<a href="{escape(ROYAL_ARCHIVE)}">Royal House archive ↗</a></p>'
        f'<p><a href="{escape(event.archive_url)}">{escape(event.archive_label)} ↗</a></p>'
    )


def render_weather(day: date, archive: WeatherArchive) -> str:
    row = archive.readings.get(day)
    if row is None or row.adjusted is None:
        return '<p class="empty">The dataset has no adjusted daily mean for this date. Try another date.</p>'
    context = {
        '1': 'Source: Stockholm manual observations.',
        '2': 'Source: an estimate from Uppsala observations adjusted to represent Stockholm.',
        '3': 'Source: SMHI automatic observations in Stockholm.',
    }[row.site]
    baseline = archive.baseline(day)
    baseline_text = ''
    if baseline:
        mean, count = baseline
        baseline_text = (f'<p class="note"><strong>{degrees(mean)}</strong> average for this calendar date, '
                         f'{BASELINE_START}–{BASELINE_END} ({count} readings)</p>')
    latest = archive.latest_match(day)
    if latest:
        modern = archive.readings[latest]
        assert modern.adjusted is not None
        comparison = (f'<p class="comparison-value">{readable_date(latest)} <strong>{degrees(modern.adjusted)}</strong></p>'
                      f'<p class="note">{direction(modern.adjusted - row.adjusted)} compared with {day.year}.</p>')
        raw_latest = f'<dt>{readable_date(latest)}</dt><dd>{degrees(modern.non_homogenised)}</dd>'
    else:
        comparison = '<p class="note">No newer reading for this calendar date in the current dataset.</p>'
        raw_latest = ''
    return (
        f'<p class="temperature">{degrees(row.adjusted)}</p>'
        f'<p class="note">Adjusted daily mean. {context}</p>{baseline_text}'
        f'<section class="comparison"><h3 class="kicker">Same date, latest year</h3>{comparison}</section>'
        f'<details><summary>See the non-homogenised values</summary><dl>'
        f'<dt>{readable_date(day)}</dt><dd>{degrees(row.non_homogenised)}</dd>{raw_latest}</dl>'
        f'<p class="note">The publisher adjusts for urban warming and other changes in the record. '
        f'<a href="{DATASET}">Read the methodology ↗</a></p></details>'
    )


StartResponse = Callable[[str, list[tuple[str, str]]], object]
WSGIApp = Callable[[Mapping[str, object], StartResponse], Iterable[bytes]]


GenerateDispatch = Callable[[dict[str, str]], str]


def dispatch_facts(day: date, archive: WeatherArchive) -> dict[str, str]:
    row = archive.readings[day]
    facts = {'date': readable_date(day), 'adjusted daily mean': degrees(row.adjusted)}
    if row.site == '2':
        facts['reading note'] = 'Estimate from Uppsala observations adjusted to represent Stockholm.'
    event = EVENTS.get(day)
    if event:
        facts['verified historical event'] = event.summary
        facts['monarch'] = 'Oskar II'
    baseline = archive.baseline(day)
    if baseline:
        mean, count = baseline
        facts['reference period'] = f'{BASELINE_START}–{BASELINE_END}, {count} readings for the same calendar date'
        facts['reference mean'] = degrees(mean)
    latest = archive.latest_match(day)
    if latest and row.adjusted is not None:
        later = archive.readings[latest].adjusted
        if later is not None:
            facts['latest same calendar date'] = readable_date(latest)
            facts['latest adjusted daily mean'] = degrees(later)
            facts['latest compared with selected date'] = direction(later - row.adjusted)
    return facts


def dispatch_query(environ: Mapping[str, object], token: str) -> str | None:
    if str(environ.get('CONTENT_TYPE', '')).split(';', 1)[0] != 'application/x-www-form-urlencoded':
        return None
    length = str(environ.get('CONTENT_LENGTH', ''))
    if not re.fullmatch(r'[0-9]{1,3}', length) or not 1 <= int(length) <= 256:
        return None
    stream = environ.get('wsgi.input')
    if stream is None:
        return None
    try:
        body = cast(BinaryIO, stream).read(int(length))
        if len(body) != int(length):
            return None
        fields = parse_qs(body.decode('ascii'), strict_parsing=True, max_num_fields=2)
        if set(fields) != {'date', 'token'} or any(len(values) != 1 for values in fields.values()):
            return None
        submitted = fields['token'][0]
        day = fields['date'][0]
        if not submitted.isascii() or not compare_digest(submitted, token) or not DATE_SHAPE.fullmatch(day):
            return None
        return f'date={day}'
    except (ValueError, OSError):
        return None


def dispatch_panel(day: date, token: str, text: str = '', error: str = '') -> str:
    if text:
        content = (
            '<p class="note">AI-generated from the weather and historical sources shown here.</p>'
            f'<p class="lede">{escape(text)}</p>'
        )
    else:
        problem = f'<p role="alert" class="error">{escape(error)}</p>' if error else ''
        content = (
            '<p class="note">Create a short AI dispatch for this date, grounded in the source facts on this page.</p>'
            f'{problem}<form action="/" method="post">'
            f'<input type="hidden" name="date" value="{day.isoformat()}">'
            f'<input type="hidden" name="token" value="{escape(token)}">'
            '<button type="submit">Create time capsule</button></form>'
        )
    return ('<section class="dispatch"><h2 class="kicker">Time capsule dispatch</h2>'
            f'{content}</section>')


def create_app(csv_path: Path, generate: GenerateDispatch | None = None) -> WSGIApp:
    archive = WeatherArchive(csv_path)
    template = Template((ROOT / 'page.html').read_text(encoding='utf-8'))
    stylesheet = (ROOT / 'reader.css').read_bytes()
    token = token_urlsafe(32) if generate else ''
    dispatches: OrderedDict[date, str] = OrderedDict()
    pending_dispatches: set[date] = set()
    dispatch_lock = Lock()

    def get_dispatch(day: date, create: GenerateDispatch | None) -> str:
        with dispatch_lock:
            if day in dispatches:
                dispatches.move_to_end(day)
                return dispatches[day]
            if create is None:
                return ''
            if day in pending_dispatches:
                raise DispatchError('A dispatch for this date is already being generated. Try again shortly.')
            pending_dispatches.add(day)

        try:
            text = create(dispatch_facts(day, archive))
            with dispatch_lock:
                if text:
                    dispatches[day] = text
                    if len(dispatches) > MAX_CACHED_DISPATCHES:
                        dispatches.popitem(last=False)
            return text
        finally:
            with dispatch_lock:
                pending_dispatches.remove(day)

    def application(environ: Mapping[str, object], start_response: StartResponse) -> Iterable[bytes]:
        method = environ.get('REQUEST_METHOD', 'GET')
        path = environ.get('PATH_INFO', '/')
        status, content_type = '200 OK', 'text/html; charset=utf-8'
        dispatch_post = method == 'POST' and path == '/' and generate is not None
        host = str(environ.get('HTTP_HOST', ''))
        if generate and not re.fullmatch(r'(?:127\.0\.0\.1|localhost)(?::[0-9]{1,5})?', host):
            status, content_type, body = '403 Forbidden', 'text/plain; charset=utf-8', b'Use the local preview address.'
        elif method not in ('GET', 'HEAD') and not dispatch_post:
            status, content_type, body = '405 Method Not Allowed', 'text/plain; charset=utf-8', b'Method not allowed.'
        elif dispatch_post and (query := dispatch_query(environ, token)) is None:
            status, content_type, body = '400 Bad Request', 'text/plain; charset=utf-8', b'Invalid request. Open the time capsule and try again.'
        elif path == '/reader.css':
            content_type, body = 'text/css; charset=utf-8', stylesheet
        elif path == '/healthz':
            content_type, body = 'text/plain; charset=utf-8', b'ok\n'
        elif path != '/':
            status, content_type, body = '404 Not Found', 'text/plain; charset=utf-8', b'Page not found.'
        else:
            day, error = parse_date(query if dispatch_post else str(environ.get('QUERY_STRING', '')), archive)
            dispatch = ''
            if error:
                status = '400 Bad Request'
                title, history, weather = 'Choose a date', '', '<p class="empty">Choose a valid date to see the reading.</p>'
            else:
                title, history = render_history(day)
                weather = render_weather(day, archive)
                reading = archive.readings.get(day)
                if generate and reading and reading.adjusted is not None:
                    try:
                        text = get_dispatch(day, generate if dispatch_post else None)
                        dispatch = dispatch_panel(day, token, text=text)
                    except DispatchError as exc:
                        status = '503 Service Unavailable'
                        dispatch = dispatch_panel(day, token, error=str(exc))
                elif dispatch_post:
                    status = '400 Bad Request'
                    dispatch = '<p class="note">Choose a date with a verified weather reading to create a time capsule.</p>'
            body = template.substitute(
                title=escape(title), display_date=escape(readable_date(day)) if not error else 'Date lookup',
                history=history, weather=weather, dispatch=dispatch, input_date=day.isoformat() if not error else '',
                min_date=archive.first.isoformat(), max_date=archive.last.isoformat(),
                coverage_start=escape(readable_date(archive.first)), coverage_end=escape(readable_date(archive.last)),
                dataset=DATASET,
                error=f'<p class="error" role="alert">{escape(error)}</p>' if error else '',
                none_selected='selected' if error or day not in EVENTS else '',
                exhibition_selected='selected' if not error and day == EXHIBITION else '',
                nobel_selected='selected' if not error and day == NOBEL else '',
            ).encode('utf-8')
        headers = [
            ('Content-Type', content_type), ('Content-Length', str(len(body))),
            ('Content-Security-Policy', "default-src 'none'; style-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"),
            ('X-Content-Type-Options', 'nosniff'), ('Referrer-Policy', 'no-referrer'),
            ('X-Frame-Options', 'DENY'),
        ]
        if generate:
            headers.append(('Cache-Control', 'no-store'))
        if status.startswith('405'):
            headers.append(('Allow', 'GET, HEAD'))
        start_response(status, headers)
        return [b'' if method == 'HEAD' else body]

    return application


def default_app(generate: GenerateDispatch | None = None) -> WSGIApp:
    path = Path(os.environ.get('CAPSULE_DAILY_CSV', str(ROOT / 'stockholm_daily_mean_temperature.csv')))
    try:
        return create_app(path, generate)
    except (OSError, ValueError, TypeError, InvalidOperation) as exc:
        raise RuntimeError('Could not load the Bolin Centre daily temperature dataset.') from exc


if __name__ == '__main__':
    import argparse
    import sys
    from getpass import getpass
    from wsgiref.simple_server import make_server

    parser = argparse.ArgumentParser(description='Run the local Time Capsule preview')
    provider = parser.add_mutually_exclusive_group()
    provider.add_argument('--bedrock', action='store_true', help='Enable a limited local Amazon Bedrock test')
    provider.add_argument('--mistral', action='store_true', help='Enable a limited local Mistral test')
    parser.add_argument('--port', type=int, default=os.environ.get('CAPSULE_PORT', '8767'))
    args = parser.parse_args()
    generator: GenerateDispatch | None = None
    if args.bedrock:
        try:
            generator = BedrockDispatch.from_environment()
        except ValueError as exc:
            parser.error(str(exc))
    elif args.mistral:
        key = os.environ.get('MISTRAL_API_KEY')
        if not key:
            if not sys.stdin.isatty():
                parser.error('Open an interactive terminal or set MISTRAL_API_KEY in that session.')
            key = getpass('Mistral API key (input hidden): ')
        try:
            generator = MistralDispatch(key)
        except ValueError as exc:
            parser.error(str(exc))
        del key

    port = args.port
    with make_server('127.0.0.1', port, default_app(generator)) as server:
        print(f'Local preview: http://127.0.0.1:{port}/', flush=True)
        server.serve_forever()
