"""Use the sales-pool card for contact recommendations and research details."""
from .sales_pools import build_sales_pools, research_card


def development(event):
    return research_card(event) or {'label':'待核实'}


def homepage_contacts(pools=None):
    if pools is None:
        pools = build_sales_pools()
    cards = sorted((card for pool in pools for card in pool['cards']),
                   key=lambda card: (-card['score'], card['company_name'], card['source_url']))
    companies = {}
    for card in cards:
        if card['grade'] == 'A':
            companies.setdefault(card['company_name'], card)
    return list(companies.values())
