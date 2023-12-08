"""London Stock Exchange helpers."""


#
# Copyright (c) 2023 LateGenXer
#
# SPDX-License-Identifier: AGPL-3.0-or-later
#


import csv
import datetime
import email.utils
import logging
import os.path
import re
import requests
import sys
import pprint

import caching

from download import download


__all__ = [
    'LSE',
]


logger = logging.getLogger('lse')


_headers = {
    'authority': 'api.londonstockexchange.com',
    'accept': 'application/json',
    'origin': 'https://www.londonstockexchange.com',
    'referer': 'https://www.londonstockexchange.com/',
    'user-agent': 'Mozilla/5.0'
}


# Tradable Instrument Display Mnemonics (TIDM)
_tidm_re = re.compile(r'^https://www\.londonstockexchange\.com/stock/(?P<tidm>\w+)/.*$')


# https://requests.readthedocs.io/en/latest/user/advanced/#keep-alive
_session = requests.Session()


@caching.cache_data(ttl=24*3600)
def lookup_tidm(isin):
    logger.info(f'Looking up TIDM of {isin}')
    # https://www.londonstockexchange.com/live-markets/market-data-dashboard/price-explorer?categories=BONDS&subcategories=14
    url = f'https://api.londonstockexchange.com/api/gw/lse/search?worlds=quotes&q={isin}'
    r = _session.get(url, headers=_headers, stream=False)
    assert r.ok

    obj = r.json()

    mo = _tidm_re.match(obj['instruments'][0]['url'])
    assert mo
    tidm = mo.group('tidm')

    return tidm


@caching.cache_data(ttl=15*60)
def get_instrument_data(tidm):
    logger.info(f'Getting {tidm} instrument data')
    url = f'https://api.londonstockexchange.com/api/gw/lse/instruments/alldata/{tidm}'
    r = _session.get(url, headers=_headers, stream=False)
    assert r.ok
    obj = r.json()
    return obj


_tidm_csv = os.path.join(os.path.dirname(__file__), 'tidm.csv')


@caching.cache_data(ttl=15*60)
def get_latest_gilt_prices():
    '''Get the latest gilt prices with a single request'''

    logger.info('Getting gilt prices from LSE')

    # https://www.londonstockexchange.com/live-markets/market-data-dashboard/price-explorer?issuers=TRIH&categories=BONDS
    # https://www.londonstockexchange.com/live-markets/market-data-dashboard/price-explorer?issuers=TRIH&categories=BONDS&page=2
    payload = {
        "path": "live-markets/market-data-dashboard/price-explorer",
        "parameters": "issuers%3DTRIH%26categories%3DBONDS",
        "components": [
            {
                "componentId": "block_content%3A9524a5dd-7053-4f7a-ac75-71d12db796b4",
                "parameters":"categories=BONDS&issuers=TRIH&size=100"
            }
        ]
    }
    headers = _headers.copy()
    headers['content-type'] = 'application/json'
    url ='https://api.londonstockexchange.com/api/v1/components/refresh'
    r = _session.post(url, headers=headers, json=payload, stream=False)
    assert r.ok
    # This can create troubles with timezones
    date = email.utils.parsedate_to_datetime(r.headers['Date'])
    obj = r.json()
    for item in obj[0]['content']:
        if item['name'] == 'priceexplorersearch':
            value = item['value']
            assert value['first'] is True
            assert value['last'] is True
            return date, value['content']
    raise ValueError  # pragma: no cover


class Prices:

    def __init__(self):
        pass

    def lookup_tidm(self, isin):  # pragma: no cover
        raise NotImplementedError

    def get_price(self, tidm):  # pragma: no cover
        raise NotImplementedError

    def get_prices_date(self):  # pragma: no cover
        raise NotImplementedError


class GiltPrices(Prices):

    def __init__(self, filename=None):
        Prices.__init__(self)
        if filename is None:
            entries = self._download()
        else:
            entries = csv.DictReader(open(filename, 'rt'))

        self.tidms = {}
        self.prices = {}

        from zoneinfo import ZoneInfo
        tzinfo = ZoneInfo("Europe/London")

        for entry in entries:
            date = datetime.date.fromisoformat(entry['date'])

            # https://www.lsegissuerservices.com/spark/lse-whitepaper-trading-insights
            self.datetime = datetime.datetime(date.year, date.month, date.day, 16, 35, 0, tzinfo=tzinfo)

            isin = entry['isin']
            tidm = entry['tidm']
            price = float(entry['price'])

            self.tidms[isin] = tidm
            self.prices[tidm] = price

    @staticmethod
    @caching.cache_data(ttl=15*60)
    def _download():
        filename = os.path.join(os.path.dirname(__file__), 'gilts-closing-prices.csv')
        download('https://lategenxer.github.io/finance/gilts-closing-prices.csv', filename)
        return list(csv.DictReader(open(filename, 'rt')))

    def lookup_tidm(self, isin):
        return self.tidms[isin]

    def get_price(self, tidm):
        return self.prices[tidm]

    def get_prices_date(self):
        return self.datetime


class TradewebClosePrices(Prices):
    # https://reports.tradeweb.com/closing-prices/gilts/ > Type: Gilts Only > Export

    default = os.path.join(os.path.dirname(__file__), 'Tradeweb_FTSE_ClosePrices_20231201.csv')

    def __init__(self, filename=default):
        Prices.__init__(self)
        self.tidms = {}
        for isin, tidm in csv.reader(open(_tidm_csv, 'rt')):
            self.tidms[isin] = tidm

        self.prices = {}
        for row in self.parse(filename):
            isin = row['ISIN']
            price = float(row['Clean Price'])
            tidm = self.tidms[isin]
            self.prices[tidm] = price
            self.datetime = datetime.datetime.strptime(row['Close of Business Date'], '%d/%m/%Y')
        self.datetime = self.datetime.replace(hour=23, minute=59, second=59)

    @staticmethod
    def parse(filename):
        for row in csv.DictReader(open(filename, 'rt', encoding='utf-8-sig')):
            if row['Type'] in ('Conventional', 'Index-linked'):
                yield row

    def lookup_tidm(self, isin):
        return self.tidms[isin]

    def get_price(self, tidm):
        return self.prices[tidm]

    def get_prices_date(self):
        return self.datetime


def main():
    logging.basicConfig(format='%(asctime)s %(name)s %(levelname)s %(message)s', level=logging.INFO)

    datetime, content = get_latest_gilt_prices()

    w = csv.writer(sys.stdout)

    th = ['date', 'isin', 'tidm', 'price']
    w.writerow(th)

    for item in content:
        tidm = item['tidm']

        data = get_instrument_data(tidm)
        assert data['currency'] == 'GBP'

        lastclosedate = data['lastclosedate']
        lastclosedate = datetime.fromisoformat(lastclosedate)
        lastclosedate = lastclosedate.date()
        lastclosedate = lastclosedate.isoformat()

        tr = [lastclosedate, data['isin'], data['tidm'], data['lastclose']]
        w.writerow(tr)


if __name__ == '__main__':
    main()
