"""
Repository for DataType-related database operations.

This repository centralizes all DataType data access operations to eliminate
direct model access from views and admin classes, following the four-layer
architecture pattern.
"""

from typing import Optional

from ..models import DataType


class DataTypeRepository:
    """Repository for DataType-related database operations"""

    def get_data_type_by_name(self, name: str) -> Optional[DataType]:
        """
        Get DataType by name.

        Args:
            name: DataType name

        Returns:
            DataType instance or None
        """
        return DataType.objects.filter(name=name).first()

    def get_string_data_type(self) -> Optional[DataType]:
        """
        Get the 'string' DataType.

        Returns:
            DataType instance or None
        """
        return DataType.objects.filter(name='string').first()
