#!/usr/bin/env python3
"""Compute the returns of a portfolio.

A document exists to describe the problem in more detail.
http://furius.ca/beancount/doc/portfolio-returns

Calculating the returns is carried out by identifying the entries whose accounts
match a regular expression that defines accounts to consider for valuation, to
compute the returns over. This set of "related accounts" must also cover the
internal flows that occur within that account, that is, the income and expense
accounts that result in the activity of the portfolio itself.

We consider three sets of accounts:

  "Assets accounts": Accounts whose balances are counted towards calculating the
    total value of the portfolio. These are asset accounts that match the
    regular expression pattern.

  "Internal flow accounts": Accounts which are not valued, but which are used to
    post internal activity of the account. These are income received as a result
    of the portfolio activity, such as dividends or realized capital gains, and
    expenses incurred as a result of carrying out activity related to the
    investment activity, such as commissions and fees. These are income and
    expenses accounts.

  "External flow accounts": Accounts that are considered external to the group
    of related accounts. These are accounts from which funds will be deposited
    or withdrawn. These deposits or withdrawals must be excluded from the
    portfolio returns. Their presence is the reason computing portfolio returns
    isn't just a trivial exercise!

"""
import argparse
import copy
import io
import re
import logging

from dateutil.parser import parse as parse_datetime

from beancount.core.amount import ZERO
from beancount.core import amount
from beancount import loader
from beancount.parser import printer
from beancount.parser import options
from beancount.core import data
from beancount.core import account_types
from beancount.core import inventory
from beancount.core import getters
from beancount.ops import prices
from beancount.utils import misc_utils


def find_matching(entries, acc_types, related_regexp):
    """Match entries and identify account groups.

    Args:
      entries: A list of directives.
      acc_types: An instance of account_types.AccountTypes
      related_regexp: A regular expression string that defines the set of
        related accounts.
    Returns:
      A list of all entries with an account matching the given pattern, and a
      triplet of account lists:
        accounts_assets: A set of the asset accounts in the related group.
        accounts_intflows: A set of the internal flow accounts in the related group.
        accounts_extflows: A set of the external flow accounts.
    """
    accounts_assets = set()
    accounts_intflows = set()
    accounts_extflows = set()
    match = re.compile(related_regexp).match

    matching_entries = []
    for entry in entries:
        if not isinstance(entry, data.Transaction):
            continue
        if any(match(posting.account) for posting in entry.postings):
            matching_entries.append(entry)

            for posting in entry.postings:
                if match(posting.account):
                    if account_types.is_income_statement_account(posting.account, acc_types):
                        accounts_intflows.add(posting.account)
                    else:
                        accounts_assets.add(posting.account)
                else:
                    accounts_extflows.add(posting.account)

    return (matching_entries, (accounts_assets,
                               accounts_intflows,
                               accounts_extflows))


def sum_balances_for_accounts(balance, entry, accounts):
    """Accumulate balance for assets postings accounts on entry.

    Args:
      balance: An instance of Inventory.
      entry: A directive (directives other than Transactions are ignored).
      accounts: A set of strings, the names of accounts whose postings to include.
    Returns:
      This destructively modifies balance and returns it.
    """
    if isinstance(entry, data.Transaction):
        for posting in entry.postings:
            if posting.account in accounts:
                balance.add_position(posting.position)
    return balance


def segment_periods(entries, accounts_assets, accounts_intflows,
                    date_begin=None, date_end=None):
    """Segment entries in terms of piecewise periods of internal flow.

    This function iterated through the given entries and computes balances at
    the beginning and end of periods without external flow entries. You should be
    able to then compute the returns from these informations.

    Args:
      entries: A list of directives. The list may contain directives other than
        than transactions as well as directives with no relation to the assets or
        internal flow accounts (the function simply ignores that which is not
        relevant).
      accounts_assets: A set of the asset accounts in the related group.
      accounts_intflows: A set of the internal flow accounts in the related group.
      date_begin: A datetime.date instance, the beginning date of the period to compute
        returns over.
      date_end: A datetime.date instance, the end date of the period to compute returns
        over.
    Returns:
      A list of period tuples, each of which contains:
        period_begin: A datetime.date instance, the first day of the period.
        period_end: A datetime.date instance, the last day of the period.
        balance_begin: An Inventory instance, the balance at the beginning of the period.
        balance_end: An Inventory instance, the balance at the end of the period.
    Raises:
      ValueError: If the dates create an impossible situation, the beginning must come before
        the requested end, if specified.
    """
    logging.info("Segmenting periods...")

    if date_begin and date_end and date_begin >= date_end:
        raise ValueError("Dates are not ordered correctly: {} >= {}".format(
            date_begin, date_end))

    accounts_related = accounts_assets | accounts_intflows
    is_external_flow_entry = lambda entry: (isinstance(entry, data.Transaction) and
                                            any(posting.account not in accounts_related
                                                for posting in entry.postings))

    # Create an iterator over the entries we care about.
    iter_entries = (entry
                    for entry in entries
                    if getters.get_entry_accounts(entry) & accounts_assets)
    entry = next(iter_entries)

    # If a beginning cut-off has been specified, skip the entries before then
    # (and make sure to accumulate the initial balance correctly).
    balance = inventory.Inventory()
    if date_begin is not None:
        period_begin = date_begin
        try:
            while True:
                if entry.date >= date_begin:
                    break
                if date_end and entry.date >= date_end:
                    break
                balance = sum_balances_for_accounts(balance, entry, accounts_assets)
                entry = next(iter_entries)
        except StopIteration:
            # No periods found! Just return an empty list.
            return [(date_begin, date_end or date_begin, balance, balance)]
    else:
        period_begin = entry.date

    # Main loop over the entries.
    periods = []
    entry_logger = misc_utils.LineFileProxy(logging.debug, '   ')
    done = False
    while True:
        balance_begin = copy.copy(balance)

        logging.debug(",-----------------------------------------------------------")
        logging.debug("Begin:   %s", period_begin)
        logging.debug("Balance: %s", balance_begin)
        logging.debug("")

        # Consume all internal flow entries, simply accumulating the total balance.
        while True:
            period_end = entry.date
            if is_external_flow_entry(entry):
                break
            if date_end and entry.date >= date_end:
                period_end = date_end
                done = True
                break
            if entry:
                printer.print_entry(entry, file=entry_logger)
            balance = sum_balances_for_accounts(balance, entry, accounts_assets)
            try:
                entry = next(iter_entries)
            except StopIteration:
                done = True
                if date_end:
                    period_end = date_end
                break
        else:
            done = True

        balance_end = copy.copy(balance)
        periods.append((period_begin, period_end, balance_begin, balance_end))

        logging.debug("Balance: %s", balance_end)
        logging.debug("End:     %s", period_end)
        logging.debug("`-----------------------------------------------------------")
        logging.debug("")

        if done:
            break

        # Absorb the balance of the external flow entry.
        assert is_external_flow_entry(entry), entry
        if entry:
            printer.print_entry(entry, file=entry_logger)
        balance = sum_balances_for_accounts(balance, entry, accounts_assets)
        try:
            entry = next(iter_entries)
        except StopIteration:
            # If there is an end date, insert that final period to cover the end
            # date, with no changes.
            if date_end:
                periods.append((period_end, date_end, balance, balance))
            break

        period_begin = period_end

    return periods


def compute_period_returns(date_begin, date_end,
                           balance_begin, balance_end, price_map):
    """Compute the returns of the given begin/end balances.

    Args:
      date_begin: A datetime.date instance, the beginning date of the period.
      date_end: A datetime.date instance, the end date of the period.
      balance_begin: An instance of the Inventory at the beginning of the period.
      balance_end: An instance of the Inventory at the end of the period.
      price_map: An instance of PriceMap as computed by prices.build_price_map().
    Returns:
      A pair of:
        returns: A dict of currency -> floating-point return for the period. The
          union of all currencies for those is returned (this is done to be able
          to evaluate and report on returns in multiple currencies).
        (mktvalue_begin, mktvalue_end): Both instances of Inventory, the balance
          of the porfolio evaluated at the market value at the beginning and end
          of the period.
    """
    # Evaluate the boundary balances at market value.
    mktvalue_begin = prices.get_inventory_market_value(balance_begin, date_begin, price_map)
    mktvalue_end = prices.get_inventory_market_value(balance_end, date_end, price_map)

    # Compute the union of all currencies. At this point, ignore currencies
    # held-at-cost and issue a warning if some are found (if the price database
    # covers all the currencies held at cost, this shuold not occur).
    currencies = set()
    single_begin = {}
    single_end = {}
    for mktvalue, single in [(mktvalue_begin, single_begin),
                             (mktvalue_end, single_end)]:
        for pos in mktvalue.get_positions():
            if pos.lot.cost:
                logging.error('Could not reduce position "%s" to its value', pos)
            else:
                currencies.add(pos.lot.currency)
                assert pos.lot.currency not in single
                single[pos.lot.currency] = pos.number

    # Now for each of the currencies, compute the returns. Handle cases where
    # the currency is not present as a zero value for that currency.
    #
    # Note: In the future, we should instead require more inforamtion about the
    # desired currency for valuation and convert all contents to a single
    # currency above, so this is not needed except to handle really odd cases.
    returns = {}
    for currency in currencies:
        begin = single_begin.get(currency, ZERO)
        end = single_end.get(currency, ZERO)
        if begin == ZERO:
            returns[currency] = 1.0
        else:
            returns[currency] = float(end / begin)

    return returns, (mktvalue_begin, mktvalue_end)


def annualize_returns(returns, date_first, date_last):
    """Annualize the return rates computed from the given date range.

    Args:
      returns: A dict of floats, the calculated returns.
      date_first: A datetime.date instance, the beginning of the period.
      date_last: A datetime.date instance, the beginning of the period.
    Returns:
      A dict of float, the returns adjusted to equivalent annual rates.
    Raises:
      ValueError: If the data includes 0-day periods to annualize
        non-trivially.
    """
    num_days = (date_last - date_first).days
    if num_days == 0:
        for currency, return_ in returns.items():
            if return_ != 1:
                raise ValueError("Invalid period for return: {} days for {}".format(
                    num_days, return_))
        exponent = 1.
    else:
        exponent = 365. / num_days
    return {currency: return_ ** exponent
            for currency, return_ in returns.items()}


def compute_returns(entries, options_map,
                    accounts_assets, accounts_intflows,
                    price_map=None,
                    date_begin=None, date_end=None):
    """Compute the returns of a portfolio of accounts defined by a regular expression.

    Args:
      entries: A list of directives that may affect the account.
      options_map: An options dict as produced by the loader.
      accounts_assets: A set of account name strings, the names of the asset accounts
        included in valuing the portfolio.
      accounts_intflow: A set of account name strings, the names of internal flow
        accounts (normally income and expenses) that aren't external flows.
      price_map: An instance of PriceMap as computed by prices.build_price_map(). If left
        to its default value of None, we derive the price_map from the entries themselves.
      date_begin: A datetime.date instance, the beginning date of the period to compute
        returns over.
      date_end: A datetime.date instance, the end date of the period to compute returns
        over.
    Returns:
      A dict of currency -> float total returns and a pair of (begin, end) datetime.date
      instances from which it was computed.
    """
    if price_map is None:
        price_map = prices.build_price_map(entries)

    accounts_related = accounts_assets | accounts_intflows

    # Predicates based on account groups determined above.
    is_external_flow_entry = lambda entry: (isinstance(entry, data.Transaction) and
                                            any(posting.account not in accounts_related
                                                for posting in entry.postings))

    # Verify that external flow entries only affect balance sheet accounts and
    # not income or expenses accounts (internal flows). We do this because we
    # want to ensure that all income and expenses are incurred against assets
    # that live within the assets group. An example of something we'd like to
    # avoid is an external flow paying for fees incurred within the account that
    # should diminish the returns of the related accounts.
    for entry in entries:
        if is_external_flow_entry(entry):
            if any(posting.account in accounts_intflows for posting in entry.postings):
                oss = io.StringIO()
                printer.print_entry(entry, file=oss)
                logging.error("External flow may not affect non-asset accounts: {}".format(
                    oss.getvalue()))

    # Segment the entries, splitting at entries with external flow and computing
    # the balances before and after. This returns all such periods with the
    # balances at their beginning and end.
    periods = segment_periods(entries, accounts_assets, accounts_intflows,
                              date_begin, date_end)

    # From the period balances, compute the returns.
    logging.info("Calculating period returns...")
    logging.info("")
    all_returns = []
    for (period_begin, period_end, balance_begin, balance_end) in periods:
        period_returns, mktvalues = compute_period_returns(period_begin, period_end,
                                                           balance_begin, balance_end,
                                                           price_map)
        mktvalue_begin, mktvalue_end = mktvalues
        all_returns.append(period_returns)

        annual_returns = (annualize_returns(period_returns, period_begin, period_end)
                          if period_end != period_begin
                          else {})

        logging.info("From %s to %s", period_begin, period_end)
        logging.info("  Begin %s => %s", balance_begin, mktvalue_begin)
        logging.info("  End   %s => %s", balance_end, mktvalue_end)
        logging.info("  Returns     %s", period_returns)
        logging.info("  Annualized  %s", annual_returns)
        logging.info("")

    # Compute the piecewise returns. Note that we have to be careful to handle
    # all available currencies.
    currencies = set(currency
                     for returns in all_returns
                     for currency in returns.keys())
    total_returns = {}
    for currency in currencies:
        total_return = 1.
        for returns in all_returns:
            total_return *= returns.get(currency, 1.)
        total_returns[currency] = total_return

    date_first = periods[0][0]
    date_last = periods[-1][1]
    return total_returns, (date_first, date_last)


def compute_returns_with_regexp(entries, options_map, related_regexp,
                                date_begin=None, date_end=None):
    """Compute the returns of a portfolio of accounts defined by a regular expression.

    Args:
      entries: A list of directives.
      options_map: An options dict as produced by the loader.
      date_begin: A datetime.date instance, the beginning date of the period to compute
        returns over.
      date_end: A datetime.date instance, the end date of the period to compute returns
        over.
    Returns:
      See compute_returns().
    """
    acc_types = options.get_account_types(options_map)
    price_map = prices.build_price_map(entries)

    # Fetch the matching entries and figure out account name groups.
    matching_entries, (accounts_assets,
                       accounts_intflows,
                       accounts_extflows) = find_matching(entries, acc_types,
                                                          related_regexp)

    logging.info('Asset accounts:')
    for account in sorted(accounts_assets):
        logging.info('  %s', account)

    logging.info('Internal flows:')
    for account in sorted(accounts_intflows):
        logging.info('  %s', account)

    logging.info('External flows:')
    for account in sorted(accounts_extflows):
        logging.info('  %s', account)
    logging.info('')

    return compute_returns(entries, options_map,
                           accounts_assets, accounts_intflows,
                           price_map,
                           date_begin, date_end)


def main():
    parse_date = lambda s: parse_datetime(s).date()
    parser = argparse.ArgumentParser()

    parser.add_argument('filename', help='Ledger filename')

    parser.add_argument('related_regexp', action='store',
                        help="A regular expression for related accounts")

    parser.add_argument('-v', '--verbose', action='store_true',
                        help="Output detailed processing information. Useful for debugging")

    parser.add_argument('--date-begin', '--begin-date', action='store', type=parse_date,
                        default=None,
                        help=("Beginning date of the period to compute returns over "
                              "(default is the first related directive)"))

    parser.add_argument('--date-end', '--end-date', action='store', type=parse_date,
                        default=None,
                        help=("End date of the period to compute returns over "
                              "(default is the last related directive)"))

    args = parser.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format='%(levelname)-8s: %(message)s')

    # Load the input file and build the price database.
    entries, errors, options_map = loader.load(args.filename)

    # Compute the returns.
    returns, (date_first, date_last) = compute_returns_with_regexp(entries,
                                                                   options_map,
                                                                   args.related_regexp,
                                                                   args.date_begin,
                                                                   args.date_end)

    # Annualize the returns.
    annual_returns = annualize_returns(returns, date_first, date_last)

    print('Total returns from {} to {}:'.format(date_first, date_last))
    for currency, return_ in sorted(returns.items()):
        print('  {}: {:.3%}'.format(currency, return_ - 1))

    print('Averaged annual returns from {} to {}:'.format(date_first, date_last))
    for currency, return_ in sorted(annual_returns.items()):
        print('  {}: {:.3%}'.format(currency, return_ - 1))


if __name__ == '__main__':
    main()


# FIXME: Issue warnings if the price date is too far from the requested market
# value date.

# On Sun, Nov 23, 2014 at 5:50 PM, Filippo Tampieri wrote:
# One suggestion: often people seem to talk about their average returns
# excluding the cash they have sitting idle; being able to include or exclude
# idle cash from the calculations would be interesting.
#
#   Ha! That's a great idea. It wouldn't be super extra hard, but I would have
#   to break the algorithm to compute periods around some internal flows (e.g. a
#   buy or a sell). It's not a trivial difference. I'll see if I can paameterize
#   the segmenting algorithm.
