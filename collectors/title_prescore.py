"""Score announcement titles once per category, before any detail read."""
import json
from pathlib import Path

from radar.china_places import CITY
from collectors.target_events import compact

RULES = json.loads((Path(__file__).resolve().parents[1] / 'config/title_prescore_rules.json').read_text(encoding='utf-8'))


def title_prescore(title, entities):
    text = compact(title)
    rules = {}
    region_text = text
    for term in RULES['japan']:
        if term in text:
            rules.setdefault('japan', []).append(term)
            region_text = region_text.replace(term, '')
    names = [name for name in entities if compact(name) in region_text]
    regions = list(dict.fromkeys(CITY.findall(region_text) + [t for t in RULES['mainland'] if t in region_text]))
    if names:
        rules['entity'] = names
    elif regions:
        rules['mainland'] = regions
    for group in ('strong','medium','weak','noise','foreign'):
        matches = [term for term in RULES[group] if term in text]
        if group == 'medium' and 'strong' in rules or group == 'foreign' and (names or regions):
            continue
        if matches:
            rules[group] = matches
    score = sum(RULES['points'].get(group, 0) for group in rules)
    return score, rules
