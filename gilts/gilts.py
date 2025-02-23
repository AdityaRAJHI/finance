#
# Copyright (c) 2023 LateGenXer
#
# SPDX-License-Identifier: AGPL-3.0-or-later
#


import csv
import datetime
import dataclasses
import enum
import logging
import math
import numbers
import operator
import os.path
import re
import sys
import typing

import xml.etree.ElementTree

from zoneinfo import ZoneInfo
from pprint import pp

from download import download

import scipy.optimize as optimize

import pandas as pd

import lp

from xirr import *
from ukcalendar import *
from data import lse
from data.tidm import load as load_tidms


logger = logging.getLogger('gilts')


tzinfo = ZoneInfo("Europe/London")


SHORT, STANDARD, LONG = -1, 0, 1


# https://www.dmo.gov.uk/responsibilities/gilt-market/about-gilts/
class Gilt:

    type_ = 'Conventional'

    def __init__(self, name, isin:str, coupon:float, maturity:datetime.date, issue_date:datetime.date):
        self.name = name
        assert isinstance(isin, str) and len(isin) == 12
        self.isin = isin
        assert isinstance(coupon, numbers.Number)
        self.coupon = coupon
        assert isinstance(maturity, datetime.date)
        self.maturity = maturity
        self.issue_date = issue_date

    # First dividends are often non-standard, being longer/shorter than 6
    # months.  The used convention is not written anywhere, but we assume
    # the first dividend period will be between 2 and 8 months.
    # XXX Another solution would be to infer the first dividend date from
    # dmo-D1A.xml's CURRENT_EX_DIV_DATE attribute.
    min_coupon_days = 60

    def coupon_dates(self, settlement_date):
        assert settlement_date >= self.issue_date
        assert settlement_date <= self.maturity
        next_coupon_dates = []
        prev_coupon_date = self.maturity
        periods = 0
        while (prev_coupon_date - self.issue_date).days >= self.min_coupon_days:
            next_coupon_dates.append(prev_coupon_date)
            periods += 1
            prev_coupon_date = shift_month(self.maturity, -6*periods)
            if prev_coupon_date < settlement_date:
                break
            prev_coupon_date = shift_month(self.maturity, -6*periods)
        next_coupon_dates.reverse()
        return prev_coupon_date, next_coupon_dates

    def prev_next_coupon_date(self, settlement_date):
        prev_coupon_date, next_coupon_dates = self.coupon_dates(settlement_date=settlement_date)
        return prev_coupon_date, next_coupon_dates[0]

    @staticmethod
    def ex_dividend_date(coupon_date):
        xd_date = coupon_date
        for i in range(7):
            xd_date = prev_business_day(xd_date)
        return xd_date

    def _period(self, prev_coupon_date):
        if (prev_coupon_date - self.issue_date).days >= self.min_coupon_days:
            return STANDARD
        elif prev_coupon_date <= self.issue_date:
            return SHORT
        else:
            return LONG

    # https://www.dmo.gov.uk/media/1sljygul/yldeqns.pdf Section 3
    # https://docs.londonstockexchange.com/sites/default/files/documents/calculator.xls
    # https://docs.londonstockexchange.com/sites/default/files/documents/accrued-interest-gilts.pdf
    def accrued_interest(self, settlement_date):
        prev_coupon_date, next_coupon_date = self.prev_next_coupon_date(settlement_date)

        dividend = self.coupon / 2.0

        full_coupon_days = (next_coupon_date - prev_coupon_date).days

        assert self.issue_date is not None
        xd_date = self.ex_dividend_date(next_coupon_date)

        period = self._period(prev_coupon_date)
        if period == STANDARD:
            # Standard dividend periods
            interest_days = (settlement_date - prev_coupon_date).days
            accrued_interest = interest_days/full_coupon_days
            if settlement_date > xd_date:
                accrued_interest -= 1
        elif period == SHORT:
            # Short first dividend period
            # See DMO's Formulae for Calculating Gilt Prices from Yields, Section 3, (2), Short first dividend periods
            interest_days = (settlement_date - self.issue_date).days
            coupon_days = (next_coupon_date - self.issue_date).days
            if settlement_date <= xd_date:
                accrued_interest = interest_days/full_coupon_days
            else:
                accrued_interest = (interest_days - coupon_days)/full_coupon_days
        else:
            assert period == LONG
            # Long first dividend period
            prev_prev_coupon_date = shift_month(prev_coupon_date, -6)
            prev_full_coupon_days = (prev_coupon_date - prev_prev_coupon_date).days
            if settlement_date < prev_coupon_date:
                interest_days = (settlement_date - self.issue_date).days
                accrued_interest = interest_days / prev_full_coupon_days
            else:
                interest_days = (settlement_date - prev_coupon_date).days
                if settlement_date <= xd_date:
                    accrued_interest = (prev_coupon_date - self.issue_date).days / prev_full_coupon_days \
                                     + interest_days / full_coupon_days
                else:
                    accrued_interest = interest_days / full_coupon_days - 1

        return accrued_interest * dividend

    def dirty_price(self, clean_price, settlement_date):
        return clean_price + self.accrued_interest(settlement_date=settlement_date)

    def clean_price(self, dirty_price, settlement_date):
        return dirty_price - self.accrued_interest(settlement_date=settlement_date)

    def cash_flows(self, settlement_date):
        if settlement_date > self.ex_dividend_date(self.maturity):
            return []

        prev_coupon_date, next_coupon_dates = self.coupon_dates(settlement_date=settlement_date)

        transactions = []

        xd_date = self.ex_dividend_date(next_coupon_dates[0])
        if settlement_date > xd_date:
            prev_coupon_date = next_coupon_dates.pop(0)
        else:
            period = self._period(prev_coupon_date)
            if period == STANDARD:
                pass
            else:
                next_coupon_date = next_coupon_dates.pop(0)
                if period == SHORT:
                    r = (next_coupon_date - self.issue_date).days / (next_coupon_date - prev_coupon_date).days
                else:
                    assert period == LONG
                    prev_prev_coupon_date = shift_month(prev_coupon_date, -6)
                    r = (prev_coupon_date - self.issue_date).days / (prev_coupon_date - prev_prev_coupon_date).days + 1

                transactions.append((next_coupon_date, r*self.coupon/2.0))

        for next_coupon_date in next_coupon_dates:
            transactions.append((next_coupon_date, self.coupon/2.0))

        transactions.append((self.maturity, 100.0))

        return transactions

    def ytm(self, dirty_price, settlement_date):
        # https://www.dmo.gov.uk/media/1sljygul/yldeqns.pdf , Section 1: price/yield formulae
        # https://www.lseg.com/content/dam/ftse-russell/en_us/documents/ground-rules/ftse-actuaries-uk-gilts-index-series-guide-to-calc.pdf Section 6, Formulae – applying to conventional gilts only

        P = dirty_price

        prev_coupon_date, next_coupon_dates = self.coupon_dates(settlement_date=settlement_date)
        next_coupon_date = next_coupon_dates[0]
        n = len(next_coupon_dates) - 1

        c = self.coupon
        f = 2.0
        d1 = c/f
        d2 = c/f

        xd_date = self.ex_dividend_date(next_coupon_date)
        if settlement_date > xd_date:
            d1 = 0
        else:
            period = self._period(prev_coupon_date)
            if period == STANDARD:
                pass
            elif period == SHORT:
                assert prev_coupon_date <= self.issue_date
                assert self.issue_date <= next_coupon_date
                d1 *= (next_coupon_date - self.issue_date).days/(next_coupon_date - prev_coupon_date).days
            else:
                assert period == LONG
                prev_prev_coupon_date = shift_month(prev_coupon_date, -6)
                d1 *= 1 + (prev_coupon_date - self.issue_date).days/(prev_coupon_date - prev_prev_coupon_date).days
                if settlement_date <= prev_coupon_date:
                    next_coupon_date = prev_coupon_date
                    prev_coupon_date = prev_prev_coupon_date
                    assert prev_coupon_date <= self.issue_date
                    assert self.issue_date <= next_coupon_date
                    n += 1
                    d2 = d1
                    d1 = 0

        assert prev_coupon_date < settlement_date
        assert settlement_date <= next_coupon_date

        r = (next_coupon_date - settlement_date).days
        assert r >= 0
        s = (next_coupon_date - prev_coupon_date).days
        assert s >= 181 and s <= 184

        if logger.isEnabledFor(logging.DEBUG):
            logger.debug('settlement_date = %s', settlement_date)
            logger.debug('xd_date = %s', xd_date)
            logger.debug('prev_coupon_date = %s', prev_coupon_date)
            logger.debug('next_coupon_date = %s', next_coupon_date)
            logger.debug('P = %f', P)
            logger.debug('n = %i', n)
            logger.debug('c = %f', c)
            logger.debug('d1 = %f', d1)
            logger.debug('d2 = %f', d2)
            logger.debug('r = %i', r)
            logger.debug('s = %i', s)

        # https://www.dmo.gov.uk/media/1sljygul/yldeqns.pdf Section 1
        if n > 0:
            def fn(v):
                return v**(r/s) * (d1 + d2 * v + c * v**2 / (f * (1 - v)) * (1 - v**(n - 1)) + 100 * v**n) - P

            v0 = 1 / (1 + .05 / f)
            v = optimize.newton(fn, v0)
            y = (1 / v - 1) * f
        else:
            y = f * (((d1 + 100) / P) ** (s/r) - 1)

        return y

    def value(self, rate, settlement_date):
        transactions = []
        transactions.append((settlement_date, 0))
        transactions.extend(self.cash_flows(settlement_date=settlement_date))

        dates, values = zip(*transactions)
        npv = xnpv(rate, values, dates)

        return npv

    def short_name(self):
        return f'{self.coupon:.3f}% {self.maturity}'


class IndexLinkedGilt(Gilt):

    type_ = 'Index-linked'

    # Default inflation assumption for estimating future cash flows
    inflation_rate = 0.03

    def __init__(self, name, isin, coupon, maturity, issue_date, base_rpi, rpi_series):
        Gilt.__init__(self, name, isin, coupon, maturity, issue_date=issue_date)
        self.base_rpi = base_rpi
        self.lag = 3 if issue_date >= datetime.date(2005, 9, 22) else 8
        self.rpi_series = rpi_series

    # https://www.dmo.gov.uk/media/0ltegugd/igcalc.pdf
    def ref_rpi(self, settlement_date, inflation_rate=None):
        if self.lag == 3:
            d = settlement_date
            month_idx = self.rpi_series.lookup_index(d)
            month_idx -= self.lag
            weight = (d.day - 1) / days_in_month(d.year, d.month)
            assert weight >= 0 and weight < 1
            rpi0 = self.rpi_series.extrapolate_from_index(month_idx,     inflation_rate)
            rpi1 = self.rpi_series.extrapolate_from_index(month_idx + 1, inflation_rate)
            ref_rpi = rpi0 + weight * (rpi1 - rpi0)
        else:
            assert self.lag == 8
            _, d = self.prev_next_coupon_date(settlement_date)
            month_idx = self.rpi_series.lookup_index(d)
            month_idx -= self.lag
            ref_rpi = self.rpi_series.extrapolate_from_index(month_idx, inflation_rate)
        return round(ref_rpi, 5)

    # https://www.dmo.gov.uk/media/1sljygul/yldeqns.pdf
    # Annex B: Method of indexation for index-linked gilts with a 3-month indexation lag
    # When does the redemption payment become known?
    def fixed_date(self, date):
        d = date.replace(day = 1)
        if self.lag == 3:
            if date.day > 1:
                d = shift_month(d, -2)
            else:
                d = shift_month(d, -3)
        else:
            assert self.lag == 8
            d = shift_month(d, -8)
        return d

    def redemption_fixed(self):
        return self.fixed_date(self.maturity)

    def is_fixed(self, date):
        return self.rpi_series.last_date() >= self.fixed_date(date)

    def is_redemption_fixed(self):
        return self.is_fixed(self.maturity)

    def index_ratio(self, settlement_date, inflation_rate=None):
        if inflation_rate is None:
            inflation_rate = self.inflation_rate
        index_ratio = self.ref_rpi(settlement_date, inflation_rate=inflation_rate) / self.base_rpi
        if self.lag == 3:
            index_ratio = round(index_ratio, 5)
        return index_ratio

    def dirty_price(self, clean_price, settlement_date):
        # For index-linked gilts with a 3-month indexation lag, the quoted price is the real clean price.
        if self.lag == 3:
            clean_price *= self.index_ratio(settlement_date)
        return clean_price + self.accrued_interest(settlement_date=settlement_date)

    def clean_price(self, dirty_price, settlement_date):
        clean_price = dirty_price - self.accrued_interest(settlement_date=settlement_date)
        if self.lag == 3:
            clean_price /= self.index_ratio(settlement_date)
        return clean_price

    def accrued_interest(self, settlement_date):
        accrued_interest = Gilt.accrued_interest(self, settlement_date=settlement_date)
        accrued_interest *= self.index_ratio(settlement_date)
        return accrued_interest

    def cash_flows(self, settlement_date, inflation_rate=None):
        if inflation_rate is None:
            inflation_rate = self.inflation_rate
        for date, value in Gilt.cash_flows(self, settlement_date=settlement_date):
            index_ratio = self.index_ratio(date, inflation_rate=inflation_rate)
            # See https://www.dmo.gov.uk/media/0ltegugd/igcalc.pdf
            # Annex: Rounding Conventions for Interest and Redemption Cash Flows for Index-linked Gilts
            value = round(value * index_ratio, 6 if self.issue_date.year >= 2002 else 4)
            yield date, value

    def ytm(self, dirty_price, settlement_date):
        # XXX: This is not the standard formula, but it's very difficult to
        # match published figures (e.g, Tradeweb) as there's lot of leeway in
        # the handling of the indexation.

        transactions = []
        transactions.append((settlement_date, -dirty_price))
        transactions.extend(self.cash_flows(settlement_date=settlement_date))

        dates, values = zip(*transactions)
        ytm = xirr(values, dates)

        return ytm

    def short_name(self):
        return f'{self.coupon:.3f}% IL {self.maturity}'


class Issued:
    # https://www.dmo.gov.uk/data/

    def __init__(self, filename=None, rpi_series=None, csv_filename=None):
        if csv_filename is not None:
            assert filename is None
            entries = self._parse_csv(csv_filename)
        elif filename is None:
            entries = self._download()
        else:
            entries = self._parse_xml(filename)

        assert rpi_series is not None
        self.rpi_series = rpi_series

        self.all = []
        for entry in entries:
            name = entry['INSTRUMENT_NAME']
            kwargs = {
                'name': name,
                'isin': entry['ISIN_CODE'],
                'coupon': self._parse_coupon(name),
                'maturity': self._parse_date(entry['REDEMPTION_DATE']),
                'issue_date': self._parse_date(entry['FIRST_ISSUE_DATE']),
            }
            type_ = entry['INSTRUMENT_TYPE'].rstrip(' ')
            if type_ == 'Conventional':
                gilt = Gilt(**kwargs)
            else:
                mo = re.match(r'^Index-linked (3|8) months$', type_)
                assert mo
                lag = int(mo.group(1))
                assert lag == 3 if kwargs['issue_date'] >= datetime.date(2005, 9, 22) else 8
                kwargs['base_rpi'] = float(entry['BASE_RPI_87'])
                kwargs['rpi_series'] = rpi_series
                gilt = IndexLinkedGilt(**kwargs)

            try:
                self.close_date = self._parse_date(entry['CLOSE_OF_BUSINESS_DATE'])
            except KeyError:
                self.close_date = None

            # Check ex-dividend dates match when testing
            if "PYTEST_CURRENT_TEST" in os.environ:
                try:
                    current_xd_date = self._parse_date(entry['CURRENT_EX_DIV_DATE'])
                except KeyError:
                    pass
                else:
                    # DMO seems to determine current/next xd-date from the next calendar day after close
                    settlement_date = self.close_date + datetime.timedelta(days=1)
                    _, next_coupon_date = gilt.prev_next_coupon_date(settlement_date)
                    assert gilt.ex_dividend_date(next_coupon_date) == current_xd_date

            self.all.append(gilt)

        self.all.sort(key=operator.attrgetter('maturity'))

        self.isin = {gilt.isin: gilt for gilt in self.all}

    @staticmethod
    def _download():
        # Cache of https://www.dmo.gov.uk/data/XmlDataReport?reportCode=D1A
        # updated daily by .github/workflows/gh-pages.yml to avoid Captchas on
        # more frequent downloads.
        filename = os.path.join(os.path.dirname(__file__), 'dmo-D1A.xml')
        download('https://lategenxer.github.io/finance/dmo-D1A.xml', filename)
        return list(Issued._parse_xml(filename))

    @staticmethod
    def _parse_xml(filename):
        stream = open(filename, 'rt')
        tree = xml.etree.ElementTree.parse(stream)
        root = tree.getroot()
        for node in root:
            yield node.attrib

    @staticmethod
    def _parse_csv(filename):
        entries = list(csv.DictReader(open(filename, 'rt')))
        for entry in entries:
            if entry['BASE_RPI_87']:
                issue_date = Issued._parse_date(entry['FIRST_ISSUE_DATE'])
                lag = 3 if issue_date >= datetime.date(2005, 9, 22) else 8
                entry['INSTRUMENT_TYPE'] = f'Index-linked {lag} months'
            else:
                entry['INSTRUMENT_TYPE'] = 'Conventional'
        return entries

    @staticmethod
    def _parse_date(string):
        return datetime.datetime.fromisoformat(string).date()

    _coupon_re = re.compile(r'^(?:(?P<units>[0-9]+) ?)?(?P<fraction>|[½¼¾⅛⅜⅝⅞]|\b1/2|\b[13]/4|\b[1357]/8)%? ')
    _fractions = {
        '':    0.000,

        '⅛':   0.125,
        '¼':   0.250,
        '⅜':   0.375,
        '½':   0.500,
        '⅝':   0.625,
        '¾':   0.750,
        '⅞':   0.875,

        '1/8': 0.125,
        '1/2': 0.250,
        '1/4': 0.250,
        '3/8': 0.375,
        '5/8': 0.625,
        '3/4': 0.750,
        '7/8': 0.875,
    }
    @classmethod
    def _parse_coupon(cls, name):
        # Derive coupon from name
        mo = cls._coupon_re.match(name)
        assert mo
        units = mo.group('units')
        fraction = mo.group('fraction')
        coupon = cls._fractions[fraction]
        if units is not None:
            coupon += float(units)
        return coupon

    __types = {
        False: ('Conventional',),
        True:  ('Index-linked',),
        None:  ('Conventional', 'Index-linked'),
    }

    def filter(self, index_linked:bool|None, settlement_date=None):
        types = self.__types[index_linked]
        for g in self.all:
            # Per https://www.dmo.gov.uk/responsibilities/gilt-market/about-gilts/ :
            # "If an investor purchases a gilt for settlement on the final day
            # of the ex-dividend period, then they will be entitled to both the
            # final dividend and the principal repayment at redemption of the
            # gilt. Trades cannot settle after the final day within the
            # ex-dividend period."
            if settlement_date is not None and settlement_date > g.ex_dividend_date(g.maturity):
                continue
            type_ = 'Conventional' if g.type_ == 'Index-linked' and g.is_redemption_fixed() else g.type_
            if type_ not in types:
                continue
            yield g


class GiltPrices:

    def __init__(self):
        self.datetime = datetime.datetime(year=datetime.MINYEAR, month=1, day=1, tzinfo=tzinfo)
        self.tidms = load_tidms()
        self.prices = {}

    def add_price(self, dt:datetime.datetime, isin:str, tidm:str, price:float):
        assert isinstance(dt, datetime.datetime)
        assert lse.is_isin(isin)
        assert lse.is_tidm(tidm)
        assert isinstance(price, numbers.Number)
        self.datetime = max(self.datetime, dt)
        self.tidms[isin] = tidm
        self.prices[tidm] = price

    @classmethod
    def from_last_close(cls, filename=None):
        if filename is None:
            entries = cls._download()
        else:
            entries = csv.DictReader(open(filename, 'rt'))

        prices = cls()
        for entry in entries:
            date = datetime.date.fromisoformat(entry['date'])

            # https://www.lsegissuerservices.com/spark/lse-whitepaper-trading-insights
            dt = datetime.datetime(date.year, date.month, date.day, 16, 35, 0, tzinfo=tzinfo)

            isin = entry['isin']
            tidm = entry['tidm']
            price = float(entry['price'])

            prices.add_price(dt, isin, tidm, price)

        return prices

    @classmethod
    def from_latest(cls, kind='midPrice'):
        assert kind in ['bid', 'offer', 'midPrice', 'lastprice', 'lastclose']
        prices = cls()
        dt, content = lse.get_latest_gilt_prices()

        # LSE prices are delayed 15min
        dt -= datetime.timedelta(minutes=15)

        for item in content:
            isin = item['isin']
            tidm = item['tidm']
            data = lse.get_instrument_data(tidm)
            try:
                assert data['tidm'] == tidm
                assert data['isin'] == isin
                assert data['currency'] == 'GBP'
                price = data[kind]
                if price is None:
                    # Gilts with low trading volume
                    price = data['lastprice']
                    if price is None:
                        # Gilts after last ex-dividend
                        continue
                prices.add_price(dt, isin, tidm, price)
            except:
                pp(data, stream=sys.stderr)
                raise
        return prices

    @staticmethod
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


def yield_curve(issued, prices, index_linked=False):
    settlement_date = next_business_day(issued.close_date)
    data = []
    for g in issued.filter(index_linked, settlement_date):
        isin = g.isin
        tidm = prices.lookup_tidm(isin)
        clean_price = prices.get_price(tidm)
        dirty_price = g.dirty_price(clean_price, settlement_date)

        ytm = g.ytm(dirty_price, settlement_date)
        if index_linked:
            ytm = (1.0 + ytm)/(1.0 + IndexLinkedGilt.inflation_rate) - 1.0
        ytm *= 100.0

        maturity = (g.maturity - issued.close_date).days / 365.25

        data.append((maturity, ytm, tidm))

    return pd.DataFrame(data, columns=['Maturity', 'Yield', 'TIDM'])



EventKind = enum.IntEnum("EventKind", ['CASH_FLOW', 'CONSUMPTION', 'TAX_YEAR_END', 'TAX_PAYMENT'])


def schedule(count, amount=10000, shift=shift_year, start=None):
    result = []
    if start is None:
        start = datetime.datetime.now(datetime.timezone.utc).date()
        for i in range(count):
            d = shift(start, 1 + i)
            result.append((d, amount))
    else:
        for i in range(count):
            d = shift(start, i)
            result.append((d, amount))
    return result


class Description:
    '''Deferred description.'''

    def __init__(self, fmt, **kwargs):
        self.fmt = fmt
        self.kwargs = kwargs

    def __str__(self):
        return self.fmt.format(**{k: lp.value(v) if isinstance(v, (lp.LpVariable, lp.LpAffineExpression)) else v for k, v in self.kwargs.items()})

    def __repr__(self):  # pragma: no cover
        return repr(self.fmt.format(**{k: math.nan if isinstance(v, (lp.LpVariable, lp.LpAffineExpression)) else v for k, v in self.kwargs.items()}))


class BondLadder:

    index_linked = False
    marginal_income_tax = 0.0
    interest_rate = 0.0
    lag = 0


    def __init__(self, issued, prices, schedule):
        self.issued = issued
        self.rpi_series = issued.rpi_series
        self.prices = prices
        self.schedule = schedule
        self.buy_df = None
        self.cash_flow_df = None
        self.today = datetime.datetime.now(datetime.timezone.utc).date()

    def solve(self):
        today = self.today
        date, amount = self.schedule[0]
        yearly_consumption = amount * 365.25 / (date - today).days

        prob = lp.LpProblem("Ladder")

        @dataclasses.dataclass
        class Event:
            date: datetime.date
            description: str
            kind: EventKind
            operand: None

        events = []
        transactions = []

        # Add consumption events
        d = today
        base_rpi = self.rpi_series.extrapolate(d, IndexLinkedGilt.inflation_rate)
        for d, amount in self.schedule:
            if self.index_linked:
                amount = amount * self.rpi_series.extrapolate(d, IndexLinkedGilt.inflation_rate) / base_rpi
            events.append(Event(d, "Withdrawal", EventKind.CONSUMPTION, amount))
            transactions.append((d, amount))
        last_consuption = d

        # Add tax events
        if self.marginal_income_tax:
            d = today.replace(month=4, day=5)
            while d < today:
                d = d.replace(year=d.year + 1)
            while True:
                tax_year = f'{d.year-1:d}/{d.year % 100:02d}'
                events.append(Event(d, f"Tax year {tax_year} end", EventKind.TAX_YEAR_END, None))
                d2 = d.replace(year=d.year + 1, month=1, day=31)
                events.append(Event(d2, f"Tax for year {tax_year}", EventKind.TAX_PAYMENT, None))
                if d >= last_consuption:
                    break
                d = d.replace(year=d.year + 1)

        initial_cash = lp.LpVariable('initial_cash', 0)
        total_cost = initial_cash

        settlement_date = next_business_day(today)

        @dataclasses.dataclass
        class Holding:
            gilt: Gilt
            tidm: str
            clean_price: float
            dirty_price: float
            initial_quantity: typing.Any

        # Add bond coupon/redemption events
        holdings = []
        for g in self.issued.filter(self.index_linked, settlement_date):
            maturity = g.maturity
            assert maturity > settlement_date
            # XXX handle this better
            if maturity > shift_year(last_consuption, self.lag):
                continue
            isin = g.isin
            tidm = self.prices.lookup_tidm(isin)
            clean_price = self.prices.get_price(tidm)

            accrued_interest = g.accrued_interest(settlement_date)
            dirty_price = g.dirty_price(clean_price, settlement_date)
            ytm = g.ytm(dirty_price, settlement_date=settlement_date)

            quantity = lp.LpVariable(tidm, 0)
            cost = quantity * dirty_price
            total_cost = total_cost + cost

            holding = Holding(g, tidm, clean_price, dirty_price, quantity)

            cash_flows = list(g.cash_flows(settlement_date))
            assert cash_flows

            consumption_dates = [d for d, v in self.schedule]

            income = quantity * -accrued_interest
            for d, amount in cash_flows[:-1]:
                if self.lag:
                    while consumption_dates and consumption_dates[0] <= d:
                        cd = consumption_dates.pop(0)
                        if maturity < shift_year(cd, self.lag) and cd <= g.ex_dividend_date(maturity):
                            sell = lp.LpVariable(f'Sell_{tidm}_{cd:%Y%m%d}', 0)
                            quantity = quantity - sell
                            prob += quantity >= 0
                            income = income + sell * g.accrued_interest(cd)
                            ref_dirty_price = g.value(rate=ytm, settlement_date=cd)
                            dirty_price = g.value(rate=0.10, settlement_date=cd)
                            discount = dirty_price/ref_dirty_price - 1
                            clean_price = g.clean_price(dirty_price, settlement_date=cd)
                            operand = sell * dirty_price, income
                            description = Description('*** Sell {sell:.2f} × {tidm} @ {clean_price:.2f} ({discount:+.1%}) ***', tidm=tidm, sell=sell, clean_price=clean_price, discount=discount)
                            events.append(Event(cd, description, EventKind.CASH_FLOW, operand))
                            income = 0

                # Coupons
                if d <= last_consuption:
                    income = income + quantity * amount
                    operand = quantity * amount, income
                    description = Description('Coupon from {quantity:.2f} × {tidm} @ {amount:.4f}', tidm=tidm, quantity=quantity, amount=amount)
                    events.append(Event(d, description, EventKind.CASH_FLOW, operand))
                    income = 0

            # Sell/redemption
            d, amount = cash_flows[-1]
            assert d == maturity
            if maturity <= last_consuption:
                assert income == 0
                operand = quantity * amount, None
                events.append(Event(maturity, f'Redemption of {tidm}', EventKind.CASH_FLOW, operand))
                quantity = 0

            holdings.append(holding)

        # Sort events chronologically
        events.sort(key=operator.attrgetter("date", "kind"))

        @dataclasses.dataclass
        class CashFlow:
            date: datetime.date
            description: str | Description
            incoming: typing.Any = math.nan
            outgoing: typing.Any = math.nan
            balance: typing.Any = math.nan
            income: typing.Any = math.nan

        cash_flows = []

        # Initial cash deposit
        balance = initial_cash
        cash_flows.append(CashFlow(date=today, description="Deposit", incoming=initial_cash, balance=balance))

        interest_desc = "Interest"

        accrued_income = 0
        tax_due = None
        prev_date = today
        for ev in events:
            #pp(event)

            # Accumulate interest on cash balance
            if ev.date != prev_date:
                assert ev.date >= prev_date
                if self.interest_rate and ev.date <= last_consuption:
                    pro_rata_interest_rate = self.interest_rate * (ev.date - prev_date).days / 365.25
                    interest = balance * pro_rata_interest_rate
                    balance = balance * (1.0 + pro_rata_interest_rate)
                    accrued_income = accrued_income + interest
                    cash_flows.append(CashFlow(date=ev.date, description=interest_desc, incoming=interest, balance=balance, income=interest))
                prev_date = ev.date


            incoming = None
            outgoing = None
            income = None
            if ev.kind == EventKind.CONSUMPTION:
                outgoing = ev.operand
            elif ev.kind == EventKind.CASH_FLOW:
                incoming, income = ev.operand
                if income is not None:
                    accrued_income = accrued_income + income
            elif ev.kind == EventKind.TAX_YEAR_END:
                assert tax_due is None
                tax_due = accrued_income * self.marginal_income_tax
                accrued_income = 0
                continue
            elif ev.kind == EventKind.TAX_PAYMENT:
                assert tax_due is not None
                outgoing = tax_due
                tax_due = None
            else:
                raise ValueError(ev.kind)

            cf = CashFlow(date=ev.date, description=ev.description)
            if incoming is not None:
                balance = balance + incoming
                cf.incoming = incoming
            if outgoing is not None:
                if False:  # pragma: no cover
                    # While simpler, this is numerically unstable
                    balance = balance - outgoing
                    prob += balance >= 0
                else:
                    # Introducing a variable avoids numerical instability
                    v = lp.LpVariable(f'balance_{ev.date:%Y%m%d}_{len(cash_flows)}', 0)
                    prob += v == balance - outgoing
                    balance = v
                cf.outgoing = outgoing
            if income is not None:
                cf.income = income
            cf.balance = balance

            cash_flows.append(cf)

        prob.checkDuplicateVars()

        solvers = lp.listSolvers(onlyAvailable=True)
        if 'PULP_CBC_CMD' in solvers:
            solver = lp.PULP_CBC_CMD(msg=0)
        else:
            assert 'COIN_CMD' in solvers
            solver = lp.COIN_CMD(msg=0)

        prob.setObjective(total_cost)

        status = prob.solve(solver)
        assert status == lp.LpStatusOptimal

        # There should be no cash left, barring rounding errors
        assert lp.value(balance) < 1.0

        assert tax_due is None
        assert not self.marginal_income_tax or lp.value(accrued_income) < 0.01

        total_cost = lp.value(total_cost)

        buy_rows = []
        for h in holdings:
            quantity = lp.value(h.initial_quantity)
            g = h.gilt
            ytm = g.ytm(h.dirty_price, settlement_date=settlement_date)
            if self.index_linked:
                ytm = (1.0 + ytm)/(1.0 + IndexLinkedGilt.inflation_rate) - 1.0
            buy_rows.append({
                'Instrument': g.short_name(),
                'TIDM': h.tidm,
                'Clean Price': h.clean_price,
                'Dirty Price': h.dirty_price,
                'GRY': ytm,
                'Quantity': quantity,
                'Cost': h.dirty_price * quantity,
            })
        buy_rows.append({
            'Instrument': 'Cash',
            'Cost': lp.value(initial_cash),
        })
        buy_rows.append({
            'Instrument': 'Total',
            'Cost': total_cost,
        })
        self.buy_df = pd.DataFrame(data=buy_rows)

        self.cost = total_cost

        # Use real values
        data = []
        prev_cf = None
        for cf in cash_flows:
            cf.description = str(cf.description)
            if self.index_linked:
                index_ratio = base_rpi / self.rpi_series.extrapolate(cf.date, IndexLinkedGilt.inflation_rate)
            else:
                index_ratio = 1.0
            cf.incoming = index_ratio * lp.value(cf.incoming)
            cf.outgoing = index_ratio * lp.value(cf.outgoing)
            cf.balance  = index_ratio * lp.value(cf.balance)
            cf.income   = index_ratio * lp.value(cf.income)

            # Filter out zero flows
            if cf.incoming <= .005:
                assert math.isnan(cf.outgoing)
                assert not cf.income > .005
                continue
            if cf.outgoing <= .005:
                assert math.isnan(cf.incoming)
                assert not cf.income > .005
                continue

            # Coalesce consecutive cash interest
            if cf.description is interest_desc and prev_cf is not None and prev_cf.description is interest_desc:
                prev_cf.date = cf.date
                prev_cf.incoming += cf.incoming
                assert math.isnan(cf.outgoing)
                prev_cf.balance = cf.balance
                prev_cf.income += cf.income
                continue

            data.append(cf)
            prev_cf = cf

        df = pd.DataFrame(data=data)

        df.rename(columns={
            'date':         'Date',
            'description':  'Description',
            'incoming':     'In',
            'outgoing':     'Out',
            'balance':      'Balance',
            'income':       'Tax. Inc.',
        }, inplace=True, errors='raise')

        self.cash_flow_df = df

        self.withdrawal_rate = yearly_consumption/total_cost

        transactions.append((settlement_date, -total_cost))

        transactions.sort(key=operator.itemgetter(0))

        dates, values = zip(*transactions)
        self.yield_ = xirr(values, dates)

    def print(self):
        print(self.buy_df.to_string(
            justify='center',
            index=False,
            na_rep='',
            float_format='{:6,.2f}'.format,
            formatters={
                'Instrument': '{:20s}'.format,
                'TIDM': '{:4s}'.format,
                'GRY': '{:6.2%}'.format,
            },
        ))

        print()

        df = self.cash_flow_df

        # https://stackoverflow.com/a/25777111
        description_len = df['Description'].str.len().max()
        description_len = max(description_len + 1, 32)

        print(df.to_string(
            justify='center',
            index=False,
            na_rep='',
            float_format='{:6,.2f}'.format,
            formatters={'Description': f'{{:<{description_len}s}}'.format},
        ))

        print()

        print(f'Withdrawal Rate: {self.withdrawal_rate:,.2%}')
        print(f'Net Yield: {self.yield_:.2%}')
