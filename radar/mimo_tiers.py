"""Explicit MiMo tiers and estimated RMB costs from reported tokens."""
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool

FLASH = 'mimo-v2.6-flash'
PRO = 'mimo-v2.6-pro'
from .pro_brief import VERSION as DEEP_PROMPT
SCREEN_PROMPT = 'sales-v03.0-flash-screen'


class FlashScreen(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    is_china_mainland_logistics_relevant: StrictBool
    company_name: str = Field(max_length=200)
    china_city: str = Field(max_length=80)
    action_type: Literal['建厂','扩产','搬迁','设点','跨境贸易','无关噪音']
    one_line_reason: str = Field(min_length=1,max_length=250)


def estimated_rmb(model, usage):
    if model not in (FLASH, PRO) or not usage:
        return None
    i, o = usage.get('prompt_tokens'), usage.get('completion_tokens')
    if type(i) is not int or type(o) is not int or min(i,o)<0:
        return None
    if model==FLASH:
        return str(Decimal(i+2*o)/1_000_000)
    cached = min(i,max(0,usage.get('cached_tokens',0)))
    return str((Decimal(3*(i-cached)+6*o)+Decimal('0.025')*cached)/1_000_000)
