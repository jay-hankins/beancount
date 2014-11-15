"""A settings class for control over the number of digits rendered.
"""
__author__ = "Martin Blais <blais@furius.ca>"

import collections
import io

from beancount.utils import misc_utils


# A constant that indicates we should be rendering at full precision.
FULL_PRECISION = 'FULL'


class DisplayContextBuilder:
    """A builder object used to construct a DisplayContext from a series of numbers.

    Attributes:
      builder_infos: A dict of currency string to BuilderCurrencyInfo instance.
    """
    class BuilderCurrencyInfo:
        """A container of information for a single currency."""
        def __init__(self):
            # A frequency distribution of precisions seen in the input file.
            self.precision_dist = misc_utils.Distribution()
            # The maximum number of digits for the integer part.
            self.max_int_digits = 1

    def __init__(self):
        self.builder_infos = collections.defaultdict(self.BuilderCurrencyInfo)

    def update(self, number, currency=None):
        """Update the builder with the given number for the given currency.

        Args:
          number: An instance of Decimal to consider for this currency.
          currency: An optional string, the currency this numbers applies to.
        """
        # Note: This method gets called on every parsed number. Performance of
        # this method is tantamount. We should optimize this later.
        num_tuple = number.as_tuple()

        # Update the precision.
        cinfo = self.builder_infos[currency]
        cinfo.precision_dist.update(num_tuple.exponent)

        # Update the maximum number of integral digits.
        num_int_digits = len(num_tuple.digits) + num_tuple.exponent
        cinfo.max_int_digits = max(cinfo.max_int_digits, num_int_digits)

    def build(self):
        """Build a display context object from the accumulated info from the numbers.

        Returns:
          An instance of DisplayContext.
        """
        dcontext = DisplayContext()
        for currency, cinfo in self.builder_infos.items():
            dist = cinfo.precision_dist
            dcontext.set_precision(-dist.mode(), currency, False)
            dcontext.set_precision_max(-dist.min(), currency, False)
        dcontext.update()
        return dcontext


class DisplayContext:
    """A class used to contain various settings that control how we output numbers.
    In particular, the precision used for each currency, and whether or not
    commas should be printed. This object is intended to be passed around to all
    functions that format numbers to strings.

    Attributes:
      commas: A boolean, whether we should render commas or not.
      signs: A boolean, whether to always render the signs.
      precision: A dict of currency to precision. A key of None provides the
        default precision. A special value of FULL_PRECISION indicates we should render
        at the natural precision for the given number.
      precision_max: Like 'precision' but for maximum number of digits.
      formats: A dict of currency to a pre-baked format string to render a
        number. (A key of None is treated as for self.precision.)
      formats_max: Like 'formats' but for maximum number of digits.
      default_format: The default display format.
    """

    def __init__(self):
        self.commas = False

        self.signs = True
        self.precision = {None: FULL_PRECISION}
        self.precision_max = {None: FULL_PRECISION}
        self.formats = {}
        self.formats_max = {}

        self.update()

    @staticmethod
    def _formats_dict(precision_dict, commas, signs):
        comma_str = ',' if commas else ''
        signs_str = ' ' if signs else ''
        formats = {}
        for currency, precision in precision_dict.items():
            if precision is FULL_PRECISION:
                fmt = '{{:{0}{1}}}'.format(signs_str, comma_str)
            else:
                fmt = '{{:{0}{1}.{2}f}}'.format(signs_str, comma_str, precision)
            formats[currency] = fmt
        default_format = formats[None]
        return formats, default_format

    def update(self):
        self.formats, self.formats_default = (
            self._formats_dict(self.precision, self.commas, self.signs))
        self.formats_max, self.formats_max_default = (
            self._formats_dict(self.precision_max, self.commas, self.signs))

    def set_commas(self, commas, update=True):
        self.commas = commas
        if update:
            self.update()

    def set_precision(self, precision, currency=None, update=True):
        assert isinstance(precision, int), precision
        self.precision[currency] = precision
        if update:
            self.update()

    def set_precision_max(self, precision, currency=None, update=True):
        assert isinstance(precision, int)
        self.precision_max[currency] = precision
        if update:
            self.update()

    def format(self, number, currency=None):
        fmt = self.formats.get(currency, self.formats_default)
        return fmt.format(number)

    def format_max(self, number, currency=None):
        fmt = self.formats_max.get(currency, self.formats_max_default)
        return fmt.format(number)

    __call__ = format

    def dump(self, file):
        max_currency_width = max(len(ccy) if ccy is not None else 1
                                 for ccy in self.precision)
        fmt = '{{:{}}}   {{:4}} | {{:24}}   {{:4}} | {{:24}}'.format(max_currency_width)
        currencies = set(self.precision) | set(self.precision_max)
        currencies.discard(None)
        for currency in [None] + sorted(currencies):
            precision = self.precision.get(currency, '')
            if precision is FULL_PRECISION:
                precision = 'FULL'
                sample = ''
            else:
                sample = self.formats.get(currency, '').format(0)

            precision_max = self.precision_max.get(currency, '')
            if precision_max is FULL_PRECISION:
                precision_max = 'FULL'
                sample_max = ''
            else:
                sample_max = self.formats_max.get(currency, '').format(0)

            print(fmt.format(currency or '*', precision, sample, precision_max, sample_max),
                  file=file)

    def __str__(self):
        oss = io.StringIO()
        self.dump(oss)
        return oss.getvalue()


# Default instance of DisplayContext to use if None is spcified.
DEFAULT_DISPLAY_CONTEXT = DisplayContext()