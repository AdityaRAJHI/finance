#
# Copyright (c) 2023 LateGenXer
#
# SPDX-License-Identifier: AGPL-3.0-or-later
#


import pytest

from datetime import date

from ukbankholidays import *



@pytest.mark.parametrize("year,month,day,result", [
    (1998,  1,  1, True),
    (2110, 12, 26, True),
    (2023, 11, 17, False),
])
def test(year,month,day,result):
    assert isukbankholiday(date(year, month, day)) == result


# https://docs.londonstockexchange.com/sites/default/files/documents/dmo-private-investor-guide-to-gilts.pdf
d = [
    (2004,  8, 26),
    (2004,  8, 27),
    (2004,  8, 31),
    (2004,  9,  1),
    (2004,  9,  2),
    (2004,  9,  3),
    (2004,  9,  6),
    (2004,  9,  7),
]
d = [(d[i], d[i + 1]) for i in range(len(d) - 1)]


@pytest.mark.parametrize("d0,d1", d, ids=lambda val: str(date(*val)))
def test_prev_business_day(d0, d1):
    d0 = date(*d0)
    d1 = date(*d1)
    assert prev_business_day(d1) == d0


@pytest.mark.parametrize("d0,d1", d, ids=repr)
def test_next_business_day(d0, d1):
    d0 = date(*d0)
    d1 = date(*d1)
    assert next_business_day(d0) == d1


def test_main():
    from ukbankholidays import main
    try:
        main()
    except ValueError:
        pytest.skip("captcha")
