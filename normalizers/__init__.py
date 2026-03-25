"""Data normalization modules."""

from normalizers.address import AddressNormalizer
from normalizers.phone import PhoneNormalizer
from normalizers.practice_areas import PracticeAreaNormalizer
from normalizers.name import NameNormalizer

__all__ = [
    'AddressNormalizer',
    'PhoneNormalizer',
    'PracticeAreaNormalizer',
    'NameNormalizer',
]
