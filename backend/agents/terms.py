"""Expand real-estate abbreviations in newly generated, buyer-facing text."""
import re

_EXPANSIONS = {
    'ARV': 'ARV (After Repair Value)',
    'MAO': 'MAO (Maximum Allowable Offer)',
    'ROI': 'ROI (Return on Investment)',
}
_PATTERN = re.compile(r'(?<!\()\b(ARV|MAO|ROI)\b(?!\s*\(\s*(?:After Repair Value|Maximum Allowable Offer|Return on Investment)\s*\))',re.I)

def expand_financial_terms(text):
    return _PATTERN.sub(lambda match:_EXPANSIONS[match.group(1).upper()],str(text))
