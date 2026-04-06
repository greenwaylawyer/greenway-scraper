"""Data normalization modules."""

from normalizers.address import AddressNormalizer
from normalizers.phone import PhoneNormalizer
from normalizers.practice_areas import PracticeAreaNormalizer
from normalizers.name import NameNormalizer
from normalizers.text import title_name, title_firm, title_address_line, title_city, title_law_school

__all__ = [
    'AddressNormalizer',
    'PhoneNormalizer',
    'PracticeAreaNormalizer',
    'NameNormalizer',
    'title_name',
    'title_firm',
    'title_address_line',
    'title_city',
    'title_law_school',
]
