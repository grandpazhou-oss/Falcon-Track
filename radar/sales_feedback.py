from collections import Counter

from django import forms

from .models import SalesFeedback


class FeedbackForm(forms.Form):
    status = forms.ChoiceField(choices=SalesFeedback.Status.choices[1:])
    reason = forms.ChoiceField(choices=SalesFeedback.Reason.choices, required=False)
    note = forms.CharField(max_length=1000, required=False)

    def clean(self):
        data = super().clean()
        if data.get('status') == 'rejected' and not data.get('reason'):
            self.add_error('reason', '退回时请选择原因。')
        elif data.get('status') != 'rejected':
            data['reason'] = ''
        return data


def field_values(record):
    return {
        'feedback_status': record.status if record else 'new',
        'feedback_reason': record.reason if record else '',
        'feedback_note': record.note if record else '',
        'feedback_at': record.updated_at.isoformat() if record else '',
    }


def annotate(groups):
    cards = [card for grade in 'ABC' for card in groups[grade]]
    rows = SalesFeedback.objects.in_bulk({c['source_url'] for c in cards}, field_name='source_url')
    for card in cards:
        record = rows.get(card['source_url'])
        display(card, record)
    return groups


def distribution(groups):
    statuses = Counter(row['feedback_status'] for grade in 'AB' for row in groups[grade])
    reasons = Counter(row['feedback_reason'] for grade in 'AB' for row in groups[grade] if row['feedback_reason'])
    return {
        'statuses': {key: statuses[key] for key, _ in SalesFeedback.Status.choices},
        'reasons': {key: reasons[key] for key, _ in SalesFeedback.Reason.choices},
    }


def display(card, record):
    card.update(field_values(record))
    card['feedback_label'] = record.get_status_display() if record else '待营业看'
    card['feedback_reason_label'] = record.get_reason_display() if record else ''
    return card


def detail_feedback(card):
    if card and card['grade'] in 'AB':
        return display(card, SalesFeedback.objects.filter(source_url=card['source_url']).first())
    return None
