"""Order of a paired performance run: which binary runs first in each slot."""

SCHEME = 'ABBA'
ROLES = ('baseline', 'candidate')


def first_role(round_index):
    """ABBA on a provider's own round index: baseline, candidate, candidate,
    baseline, repeated. Over every block of four rounds each binary runs
    first twice and the two rounds where it runs second sit between them, so
    a drift that is linear in time moves both binaries' samples alike."""
    return 'baseline' if round_index % 4 in (0, 3) else 'candidate'


def paired_order(rounds, providers):
    """The order of every (round, provider) slot of a paired run.

    Every provider follows ABBA on its own round index, so each provider (and
    each of its operations) runs first equally often as baseline and as
    candidate. Rounds must be a multiple of four for that balance to hold.
    """
    if rounds <= 0 or rounds % 4:
        raise ValueError('a paired performance run needs a positive multiple of four rounds')
    return [{'round': iteration, 'provider': provider, 'first': first_role(iteration)}
            for iteration in range(rounds) for provider in providers]


def validate_pairing(baseline, candidate, providers):
    """Both reports of a paired run, or neither: a present field is checked,
    never read as the absence of pairing."""
    present = ['paired_measurement' in report for report in (baseline, candidate)]
    if not any(present):
        return False
    if not all(present):
        raise ValueError('only one performance report declares a paired run')
    pairs = [baseline['paired_measurement'], candidate['paired_measurement']]
    for role, pair, own, other in (('baseline', pairs[0], baseline, candidate),
                                   ('candidate', pairs[1], candidate, baseline)):
        if (not isinstance(pair, dict) or set(pair) != {'role', 'order_scheme', 'partner_campaign_id', 'order'}
                or pair['role'] != role or pair['order_scheme'] != SCHEME
                or pair['partner_campaign_id'] != other['campaign_id']
                or other['campaign_id'] == own['campaign_id']):
            raise ValueError('paired performance metadata is malformed')
    expected = paired_order(baseline['rounds'], providers)
    if pairs[0]['order'] != expected or pairs[1]['order'] != expected:
        raise ValueError('paired performance order differs from the ABBA order of its rounds and providers')
    return True
