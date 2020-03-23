import copy
import datetime
import functools
import random
from collections import OrderedDict
from contextlib import contextmanager

import pytest
import pytest_twisted
import pytz
from mock import ANY, MagicMock, Mock, PropertyMock, call, patch
from twisted.internet import defer
from twisted.internet.task import Clock

from cardinal.bot import CardinalBot, user_info
from plugins.ticker import plugin
from plugins.ticker.plugin import (
    TickerPlugin,
    colorize,
    est_now,
    get_delta,
    sleep,
)


def get_fake_now(market_is_open=True):
    tz = pytz.timezone('America/New_York')
    fake_now = datetime.datetime.now(tz)
    if market_is_open:
        # Ensure it is open
        fake_now = fake_now.replace(hour=10)
        while fake_now.weekday() >= 5:
            fake_now = fake_now - datetime.timedelta(days=1)
    else:
        # Ensure it is closed
        fake_now = fake_now.replace(hour=18)

    return fake_now


@contextmanager
def mock_api(response,
             fake_now=None,
             raise_times=0,
             throttle_times=0):
    fake_now = fake_now or get_fake_now()
    responses = copy.deepcopy(response) \
        if isinstance(response, list) else \
        [copy.deepcopy(response)]

    response_mock = MagicMock()
    type(response_mock).status_code = PropertyMock(return_value=200)

    # hack since nonlocal doesn't exist in py2
    context = {'raise_times': raise_times, 'throttle_times': throttle_times}
    def mock_deferToThread(*args, **kwargs):
        if context['raise_times'] > 0:
            context['raise_times'] -= 1
            raise Exception('mock exception')

        elif context['throttle_times'] > 0:
            context['throttle_times'] -= 1
            response_mock.json.return_value = make_throttle_response()

        else:
            response_mock.json.return_value = responses.pop(0)

        return response_mock

    with patch.object(plugin, 'deferToThread') as mock_defer, \
            patch.object(plugin, 'est_now', return_value=fake_now):
        mock_defer.side_effect = mock_deferToThread

        yield mock_defer


def make_throttle_response():
    return {
        "Note": "Thank you for using Alpha Vantage! Our standard API call "
            "frequency is 5 calls per minute and 500 calls per day. "
            "Please visit https://www.alphavantage.co/premium/ if you would "
            "like to target a higher API call frequency."
    }


def make_time_series_daily_response(symbol,
                                    last_open=None,
                                    last_close=None,
                                    previous_close=None,
                                    start=None):
    """Mock response for TIME_SERIES_DAILY API"""

    last_market_day = start or datetime.datetime.today()
    while last_market_day.weekday() >= 5:
        last_market_day = last_market_day - datetime.timedelta(days=1)

    def make_random_response():
        open_ = random.randrange(95, 105) + random.random()
        high = random.randrange(106, 110) + random.random()
        low = random.randrange(90, 94) + random.random()
        close = random.randrange(95, 105) + random.random()
        volume = random.randrange(100000, 999999)
        return {
            "1. open": "{:.4f}".format(open_),
            "2. high": "{:.4f}".format(high),
            "3. low": "{:.4f}".format(low),
            "4. close": "{:.4f}".format(close),
            "5. volume": "{}".format(volume)
        }

    time_series_daily = {}
    market_day = last_market_day
    previous_market_day = last_market_day - datetime.timedelta(days=1)
    for days in xrange(0, 60):
        # don't add data for weekends
        if market_day.weekday() < 5:
            response = make_random_response()

            # Override last open / last close for testing
            if market_day == last_market_day:
                if last_open:
                    response["1. open"] = "{:.4f}".format(last_open)
                if last_close:
                    response["4. close"] = "{:.4f}".format(last_close)
            if market_day == previous_market_day and previous_close:
                response["4. close"] = "{:.4f}".format(previous_close)

            time_series_daily[market_day.strftime("%Y-%m-%d")] = response
        market_day = market_day - datetime.timedelta(days=1)

    compact = True
    return {
        "Meta Data": {
            "1. Information": "Daily Prices (open, high, low, close) and "
                              "Volumes",
            "2. Symbol": symbol,
            "3. Last Refreshed": datetime.datetime.now().strftime("%Y-%m-%d"),
            "4. Output Size": "Compact" if compact else "Full size",
            "5. Time Zone": "US/Eastern"
        },
        "Time Series (Daily)": time_series_daily,
    }


def test_get_delta():
    assert get_delta(105, 100) == 5.0
    assert get_delta(95, 100) == -5.0
    assert get_delta(100, 100) == 0


def test_colorize():
    assert colorize(-0.151) == '\x0304-0.15%\x03'
    assert colorize(-0.1) == '\x0304-0.10%\x03'
    assert colorize(0) == '\x03040.00%\x03'
    assert colorize(0.1) == '\x03090.10%\x03'
    assert colorize(0.159) == '\x03090.16%\x03'

@defer.inlineCallbacks
def test_sleep():
    now = datetime.datetime.now()
    yield sleep(1)
    delta = datetime.datetime.now() - now
    assert delta.seconds == 1


class TestTickerPlugin(object):
    def setup_method(self, method):
        self.api_key = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
        self.channel = '#test'
        self.channels = [self.channel]
        self.stocks = OrderedDict({
            'INX': 'S&P 500',
            'DJI': 'Dow',
            'VEU': 'Foreign',
            'AGG': 'US Bond',
        })
        self.relay_bots = [
            {"nick": "relay.bot", "user": "relay", "vhost": "relay"},
        ]

        self.mock_cardinal = Mock(spec=CardinalBot)

        self.plugin = TickerPlugin(self.mock_cardinal, {
            'api_key': self.api_key,
            'channels': self.channels,
            'stocks': self.stocks,
            'relay_bots': self.relay_bots,
        })
        assert len(self.plugin.predictions) == 0

    def test_config_defaults(self):
        plugin = TickerPlugin(self.mock_cardinal, {
            'api_key': self.api_key,
        })
        assert plugin.config['api_key'] == self.api_key
        assert plugin.config['channels'] == []
        assert plugin.config['stocks'] == {}
        assert plugin.config['relay_bots'] == []
        assert plugin.predictions == {}

    def test_missing_api_key(self):
        with pytest.raises(KeyError):
            TickerPlugin(self.mock_cardinal, {})

    def test_missing_stocks(self):
        with pytest.raises(ValueError):
            TickerPlugin(self.mock_cardinal, {
                'api_key': self.api_key,
                'stocks': {
                    'a': 'a',
                    'b': 'b',
                    'c': 'c',
                    'd': 'd',
                    'e': 'e',
                    'f': 'f',
                },
            })

    @defer.inlineCallbacks
    def test_send_ticker(self):
        responses = [
            make_time_series_daily_response('DJI',
                                            last_open=100,
                                            last_close=200),
            make_time_series_daily_response('AGG',
                                            last_open=100,
                                            last_close=150.50),
            make_time_series_daily_response('VEU',
                                            last_open=100,
                                            last_close=105),
            make_time_series_daily_response('INX',
                                            last_open=100,
                                            last_close=50),
        ]

        with mock_api(responses, fake_now=get_fake_now(market_is_open=True)):
            yield self.plugin.send_ticker()

        self.mock_cardinal.sendMsg.assert_called_once_with(
            self.channel,
            'Dow (\x02DJI\x02): \x0309100.00%\x03 | '
            'US Bond (\x02AGG\x02): \x030950.50%\x03 | '
            'Foreign (\x02VEU\x02): \x03095.00%\x03 | '
            'S&P 500 (\x02INX\x02): \x0304-50.00%\x03'
        )

    @pytest.mark.parametrize("dt,should_send_ticker,should_do_predictions", [
        (datetime.datetime(2020, 3, 21, 16, 0, 0),  # Saturday 4pm
         False,
         False,),
        (datetime.datetime(2020, 3, 22, 16, 0, 0),  # Sunday 4pm
         False,
         False,),
        (datetime.datetime(2020, 3, 23, 15, 45, 45),  # Monday 3:45pm
         True,
         False,),
        (datetime.datetime(2020, 3, 23, 16, 0, 30),  # Monday 4pm
         True,
         True,),
        (datetime.datetime(2020, 3, 23, 16, 15, 0),  # Monday 4:15pm
         False,
         False,),
        (datetime.datetime(2020, 3, 27, 9, 15, 0),  # Friday 9:15am
         False,
         False,),
        (datetime.datetime(2020, 3, 27, 9, 30, 15),  # Friday 9:30am
         True,
         True,),
        (datetime.datetime(2020, 3, 27, 9, 45, 15),  # Friday 9:45am
         True,
         False,),
    ])
    @patch.object(plugin.TickerPlugin, 'do_predictions')
    @patch.object(plugin.TickerPlugin, 'send_ticker')
    @patch.object(plugin, 'sleep')
    @patch.object(plugin, 'est_now')
    @pytest_twisted.inlineCallbacks
    def test_tick(self,
                  est_now,
                  sleep,
                  send_ticker,
                  do_predictions,
                  dt,
                  should_send_ticker,
                  should_do_predictions):
        est_now.return_value = dt

        yield self.plugin.tick()

        if should_send_ticker:
            send_ticker.assert_called_once_with()
        else:
            assert send_ticker.mock_calls == []

        if should_do_predictions:
            sleep.assert_called_once_with(60)
            do_predictions.assert_called_once_with()
        else:
            assert sleep.mock_calls == []
            assert do_predictions.mock_calls == []

    @pytest.mark.parametrize("market_is_open", [True, False])
    @patch.object(plugin, 'reactor', new_callable=Clock)
    @pytest_twisted.inlineCallbacks
    def test_do_predictions(self, mock_reactor, market_is_open):
        symbol = 'INX'
        base = 100.0

        user1 = 'user1'
        user2 = 'user2'
        prediction1 = 105.0
        prediction2 = 96.0

        actual = 95.0

        yield self.plugin.save_prediction(
            symbol,
            user1,
            base,
            prediction1,
        )
        yield self.plugin.save_prediction(
            symbol,
            user2,
            base,
            prediction2,
        )

        assert len(self.plugin.predictions) == 1
        assert len(self.plugin.predictions[symbol]) == 2

        kwargs = {"last_open": actual} \
            if market_is_open else \
            {"last_close": actual}
        response = make_time_series_daily_response(symbol, **kwargs)

        with mock_api(response, fake_now=get_fake_now(market_is_open)) \
                as defer_mock:
            d = self.plugin.do_predictions()
            mock_reactor.advance(15)

            yield d

        assert len(self.mock_cardinal.sendMsg.mock_calls) == 3
        self.mock_cardinal.sendMsg.assert_called_with(
            self.channel,
            '{} had the closest guess for \x02{}\x02 out of {} predictions '
            'with a prediction of {} (\x0304{:.2f}%\x03) '
            'compared to the actual {} of {} (\x0304{:.2f}%\x03).'.format(
                user2,
                symbol,
                2,
                prediction2,
                -4,
                'open' if market_is_open else 'close',
                actual,
                -5))

    @patch.object(plugin, 'est_now')
    def test_send_prediction(self, mock_now):
        prediction = 105
        actual = 110
        base = 100
        nick = "nick"
        symbol = "INX"

        # Set the datetime to a known value so the message can be tested
        tz = pytz.timezone('America/New_York')
        mock_now.return_value = tz.localize(
            datetime.datetime(2020, 3, 20, 10, 50, 0, 0))

        self.plugin.save_prediction(symbol, nick, base, prediction)
        self.plugin.send_prediction(nick, symbol, actual)

        message = "Prediction by nick for \x02INX\02: 105 (\x03095.00%\x03). " \
                  "Actual value at open: 110 (\x030910.00%\x03). " \
                  "Prediction set at 2020-03-20 10:50:00 EDT."
        self.mock_cardinal.sendMsg.assert_called_once_with('#test', message)

    @pytest.mark.skip(reason="Not written yet")
    def test_check(self):
        pass

    @pytest.mark.parametrize("symbol,input_msg,output_msg,market_is_open", [
        ("INX",
         "!predict INX +5%",
         "Prediction by nick for \x02INX\x02 at market close: 105.00 (\x03095.00%\x03) ",
         True,
         ),
        ("INX",
         "!predict INX -5%",
         "Prediction by nick for \x02INX\x02 at market close: 95.00 (\x0304-5.00%\x03) ",
         True,
         ),
        ("INX",
         "!predict INX -5%",
         "Prediction by nick for \x02INX\x02 at market open: 95.00 (\x0304-5.00%\x03) ",
         False,
         ),
        # testing a few more formats of stock symbols
        ("^RUT",
         "!predict ^RUT -5%",
         "Prediction by nick for \x02^RUT\x02 at market open: 95.00 (\x0304-5.00%\x03) ",
         False,
         ),
        ("REE.MC",
         "!predict REE.MC -5%",
         "Prediction by nick for \x02REE.MC\x02 at market open: 95.00 (\x0304-5.00%\x03) ",
         False,
         ),
        ("LON:HDLV",
         "!predict LON:HDLV -5%",
         "Prediction by nick for \x02LON:HDLV\x02 at market open: 95.00 (\x0304-5.00%\x03) ",
         False,
         ),
    ])
    @pytest_twisted.inlineCallbacks
    def test_predict_market_open(self,
                                 symbol,
                                 input_msg,
                                 output_msg,
                                 market_is_open):
        channel = "#finance"

        fake_now = get_fake_now(market_is_open=market_is_open)

        kwargs = {'last_open': 100} if market_is_open else {'last_close': 100}
        response = make_time_series_daily_response(symbol, **kwargs)

        with mock_api(response, fake_now=fake_now):
            yield self.plugin.predict(self.mock_cardinal,
                                      user_info("nick", "user", "vhost"),
                                      channel,
                                      input_msg)

        assert symbol in self.plugin.predictions
        assert len(self.plugin.predictions[symbol]) == 1

        self.mock_cardinal.sendMsg.assert_called_once_with(
            channel,
            output_msg)

    @pytest.mark.parametrize("message_pairs", [
        (("!predict INX +5%",
          "Prediction by nick for \x02INX\x02 at market close: 105.00 (\x03095.00%\x03) ",
         ),
         ("!predict INX -5%",
          "Prediction by nick for \x02INX\x02 at market close: 95.00 (\x0304-5.00%\x03) "
          "(replaces old prediction of 105.00 (\x03095.00%\x03) set at {})"
         ),
         )
    ])
    @pytest_twisted.inlineCallbacks
    def test_predict_replace(self, message_pairs):
        channel = "#finance"
        symbol = 'INX'

        response = make_time_series_daily_response(symbol, last_open=100)

        fake_now = get_fake_now()
        for input_msg, output_msg in message_pairs:
            with mock_api(response, fake_now):
                yield self.plugin.predict(self.mock_cardinal,
                                          user_info("nick", "user", "vhost"),
                                          channel,
                                          input_msg)

                assert symbol in self.plugin.predictions
                assert len(self.plugin.predictions[symbol]) == 1

                self.mock_cardinal.sendMsg.assert_called_with(
                    channel,
                    output_msg.format(fake_now.strftime('%Y-%m-%d %H:%M:%S %Z'))
                    if '{}' in output_msg else
                    output_msg)

    @pytest.mark.parametrize("input_msg,output_msg", [
        ("<nick> !predict INX +5%",
         "Prediction by nick for \x02INX\x02 at market close: 105.00 (\x03095.00%\x03) ",
         ),
        ("<nick> !predict INX -5%",
         "Prediction by nick for \x02INX\x02 at market close: 95.00 (\x0304-5.00%\x03) ",
         ),
    ])
    @pytest_twisted.inlineCallbacks
    def test_predict_relay_bot(self, input_msg, output_msg):
        symbol = 'INX'
        channel = "#finance"

        response = make_time_series_daily_response(symbol, last_open=100)
        with mock_api(response):
            yield self.plugin.predict(self.mock_cardinal,
                                      user_info("relay.bot", "relay", "relay"),
                                      channel,
                                      input_msg)

        assert symbol in self.plugin.predictions
        assert len(self.plugin.predictions[symbol]) == 1

        self.mock_cardinal.sendMsg.assert_called_once_with(
            channel,
            output_msg)

    @pytest.mark.parametrize("input_msg", [
        "<whoami> !predict INX +5%",
        "<whoami> !predict INX -5%",
    ])
    @pytest_twisted.inlineCallbacks
    def test_predict_not_relay_bot(self, input_msg):
        channel = "#finance"

        yield self.plugin.predict(self.mock_cardinal,
                                  user_info("nick", "user", "vhost"),
                                  channel,
                                  input_msg)

        assert len(self.plugin.predictions) == 0
        assert self.mock_cardinal.sendMsg.mock_calls == []

    @pytest.mark.parametrize("user,message,value,expected", [
        (
            user_info("whoami", None, None),
            "!predict INX 5%",
            100,
            ("whoami", "INX", 105, 100),
        ),
        (
            user_info("whoami", None, None),
            "!predict INX +5%",
            100,
            ("whoami", "INX", 105, 100),
        ),
        (
            user_info("whoami", None, None),
            "!predict INX -5%",
            100,
            ("whoami", "INX", 95, 100),
        ),
        (
            user_info("not.a.relay.bot", None, None),
            "<whoami> !predict INX -5%",
            100,
            None,
        ),
        (
            user_info("relay.bot", "relay", "relay"),
            "<whoami> !predict INX -5%",
            100,
            ("whoami", "INX", 95, 100),
        ),
    ])
    @pytest_twisted.inlineCallbacks
    def test_parse_prediction_open(
            self,
            user,
            message,
            value,
            expected,
    ):
        symbol = 'INX'

        response = make_time_series_daily_response(symbol, last_open=value)
        with mock_api(response):
            result = yield self.plugin.parse_prediction(user, message)

        assert result == expected

    @pytest.mark.parametrize("user,message,value,expected", [
        (
            user_info("whoami", None, None),
            "!predict INX 5%",
            100,
            ("whoami", "INX", 105, 100),
        ),
        (
            user_info("whoami", None, None),
            "!predict INX +5%",
            100,
            ("whoami", "INX", 105, 100),
        ),
        (
            user_info("whoami", None, None),
            "!predict INX -5%",
            100,
            ("whoami", "INX", 95, 100),
        ),
        (
            user_info("not.a.relay.bot", None, None),
            "<whoami> !predict INX -5%",
            100,
            None,
        ),
        (
            user_info("relay.bot", "relay", "relay"),
            "<whoami> !predict INX -5%",
            100,
            ("whoami", "INX", 95, 100),
        ),
    ])
    @pytest_twisted.inlineCallbacks
    def test_parse_prediction_close(
            self,
            user,
            message,
            value,
            expected,
    ):
        symbol = 'INX'

        response = make_time_series_daily_response(symbol, last_close=value)

        with mock_api(response, fake_now=get_fake_now(market_is_open=False)):
            result = yield self.plugin.parse_prediction(user, message)

        assert result == expected

    def test_save_prediction(self):
        symbol = 'INX'
        nick = 'whoami'
        base = 100
        prediction = 105

        self.plugin.save_prediction(
            symbol,
            nick,
            base,
            prediction,
        )

        assert symbol in self.plugin.predictions
        assert nick in self.plugin.predictions[symbol]
        dt, saved_base, saved_prediction = self.plugin.predictions[symbol][nick]
        assert isinstance(dt, datetime.datetime)
        assert saved_base == base
        assert saved_prediction == prediction

    @defer.inlineCallbacks
    def test_get_daily(self):
        symbol = 'INX'
        last_open = 100.0
        last_close = 101.0
        previous_close = 102.0

        response = make_time_series_daily_response(symbol,
                                                   last_open=last_open,
                                                   last_close=last_close,
                                                   previous_close=previous_close,
                                                   )

        # Calculate expected value using randomly created response
        today = datetime.date.today().strftime('%Y-%m-%d')
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)) \
            .strftime('%Y-%m-%d')

        expected = {
            'symbol': symbol,
            'close': last_close,
            'open': last_open,
            'change': get_delta(last_close, last_open),
        }

        with mock_api(response):
            result = yield self.plugin.get_daily(symbol)
        assert result == expected

    @defer.inlineCallbacks
    def test_get_daily_missing_days(self):
        symbol = 'INX'
        last_open = 100.0
        last_close = 101.0
        previous_close = 102.0

        fake_now = datetime.datetime(
            2020,
            3,
            22,  # a Sunday
            12,
            0,
            0,
        )

        response = make_time_series_daily_response(symbol,
                                                   last_open=last_open,
                                                   last_close=last_close,
                                                   previous_close=previous_close,
                                                   start=fake_now,
                                                   )

        expected = {
            'symbol': symbol,
            'close': last_close,
            'open': last_open,
            'change': get_delta(last_close, last_open),
        }

        with mock_api(response, fake_now):
            result = yield self.plugin.get_daily(symbol)
        assert result == expected

    @defer.inlineCallbacks
    def test_get_time_series_daily(self):
        symbol = 'INX'
        outputsize = 'compact'

        response = make_time_series_daily_response(symbol)
        with mock_api(response) as defer_mock:
            result = yield self.plugin.get_time_series_daily(symbol)

        for date in response['Time Series (Daily)']:
            assert date in result
            # verify prefix is stripped and values are floats
            for key in ('open', 'high', 'low', 'close', 'volume'):
                assert key in result[date]
                assert isinstance(result[date][key], float)

    @defer.inlineCallbacks
    def test_get_time_series_daily_bad_format(self):
        symbol = 'INX'
        outputsize = 'compact'

        response = {}
        with mock_api(response) as defer_mock:
            with pytest.raises(KeyError):
                result = yield self.plugin.get_time_series_daily(symbol)

    @defer.inlineCallbacks
    def test_make_av_request(self):
        # Verify that this returns the response unmodified, and that it
        # properly calculates params
        function = 'TIME_SERIES_DAILY'
        symbol = 'INX'
        outputsize = 'compact'

        response = make_time_series_daily_response(symbol)
        with mock_api(response) as defer_mock:
            result = yield self.plugin.make_av_request(
                function,
                params={
                    'symbol': symbol,
                    'outputsize': outputsize,
                })

        assert result == response

        defer_mock.assert_called_once_with(
            plugin.requests.get,
            plugin.AV_API_URL,
            params={
                'apikey': self.api_key,
                'function': function,
                'symbol': symbol,
                'outputsize': outputsize,
                'datatype': 'json',
            })

    @defer.inlineCallbacks
    def test_make_av_request_no_params(self):
        # This one is mostly just for coverage
        function = 'TIME_SERIES_DAILY'
        symbol = 'INX'
        outputsize = 'compact'

        response = make_time_series_daily_response(symbol)
        with mock_api(response) as defer_mock:
            result = yield self.plugin.make_av_request(function)

        assert result == response

        defer_mock.assert_called_once_with(
            plugin.requests.get,
            plugin.AV_API_URL,
            params={
                'apikey': self.api_key,
                'function': function,
                'datatype': 'json',
            })

    @patch.object(plugin, 'reactor', new_callable=Clock)
    @defer.inlineCallbacks
    def test_make_av_request_retry_when_throttled(self, mock_reactor):
        # Verify that this returns the response unmodified, and that it
        # properly calculates params
        function = 'TIME_SERIES_DAILY'
        symbol = 'INX'
        outputsize = 'compact'

        response = make_time_series_daily_response(symbol)
        throttle_times = plugin.MAX_RETRIES - 1
        with mock_api(response, throttle_times=throttle_times) as defer_mock:
            d = self.plugin.make_av_request(
                function,
                params={
                    'symbol': symbol,
                    'outputsize': outputsize,
                })

            # loop through retries
            for _ in range(throttle_times):
                mock_reactor.advance(plugin.RETRY_WAIT)

            result = yield d

        assert result == response

        defer_mock.assert_has_calls([call(
            plugin.requests.get,
            plugin.AV_API_URL,
            params={
                'apikey': self.api_key,
                'function': function,
                'symbol': symbol,
                'outputsize': outputsize,
                'datatype': 'json',
            })] * (throttle_times + 1))

    @patch.object(plugin, 'reactor', new_callable=Clock)
    @defer.inlineCallbacks
    def test_make_av_request_retry_on_exception(self, mock_reactor):
        # Verify that this returns the response unmodified, and that it
        # properly calculates params
        function = 'TIME_SERIES_DAILY'
        symbol = 'INX'
        outputsize = 'compact'

        response = make_time_series_daily_response(symbol)
        raise_times = plugin.MAX_RETRIES - 1
        with mock_api(response, raise_times=raise_times) as defer_mock:
            d = self.plugin.make_av_request(
                function,
                params={
                    'symbol': symbol,
                    'outputsize': outputsize,
                })

            # loop through retries
            for _ in range(raise_times):
                mock_reactor.advance(plugin.RETRY_WAIT)

            result = yield d

        assert result == response

        defer_mock.assert_has_calls([call(
            plugin.requests.get,
            plugin.AV_API_URL,
            params={
                'apikey': self.api_key,
                'function': function,
                'symbol': symbol,
                'outputsize': outputsize,
                'datatype': 'json',
            })] * (raise_times + 1))

    @patch.object(plugin, 'reactor', new_callable=Clock)
    @defer.inlineCallbacks
    def test_make_av_request_give_up_after_max_retries(self, mock_reactor):
        # Verify that this returns the response unmodified, and that it
        # properly calculates params
        function = 'TIME_SERIES_DAILY'
        symbol = 'INX'
        outputsize = 'compact'

        response = make_time_series_daily_response(symbol)
        raise_times = plugin.MAX_RETRIES
        with mock_api(response, raise_times=raise_times) as defer_mock:
            d = self.plugin.make_av_request(
                function,
                params={
                    'symbol': symbol,
                    'outputsize': outputsize,
                })

            # loop through retries
            for _ in range(raise_times):
                mock_reactor.advance(plugin.RETRY_WAIT)

            with pytest.raises(Exception):
                result = yield d

        defer_mock.assert_has_calls([call(
            plugin.requests.get,
            plugin.AV_API_URL,
            params={
                'apikey': self.api_key,
                'function': function,
                'symbol': symbol,
                'outputsize': outputsize,
                'datatype': 'json',
            })] * (raise_times))

    @patch.object(plugin, 'est_now')
    def test_market_is_open(self, mock_now):
        tz = pytz.timezone('America/New_York')

        # Nothing special about this time - it's a Thursday 7:49pm
        mock_now.return_value = tz.localize(datetime.datetime(
            2020,
            3,
            19,
            19,
            49,
            55,
            0,
        ))
        assert False == plugin.market_is_open()

        # The market was open earlier though
        mock_now.return_value = tz.localize(datetime.datetime(
            2020,
            3,
            19,
            13,
            49,
            55,
            0,
        ))
        assert True == plugin.market_is_open()

        # But not before 9:30am
        mock_now.return_value = tz.localize(datetime.datetime(
            2020,
            3,
            19,
            9,
            29,
            59,
            0,
        ))
        assert False == plugin.market_is_open()

        # Or this weekend
        mock_now.return_value = tz.localize(datetime.datetime(
            2020,
            3,
            14,
            13,
            49,
            55,
            0,
        ))
        assert False == plugin.market_is_open()
