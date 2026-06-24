"""
Conditional Validator Module

Validates the structure of conditional logic expressions used in show_if/enabled_if properties.

Format:
{
    "logic": "and|or",
    "conditions": [
        {
            "left": {"field": "campo_x"} | {"value": 5},
            "op": "==|!=|>|>=|<|<=|in|not_in",
            "right": {"field": "campo_y"} | {"value": 10}
        }
    ]
}
"""

from typing import Dict, Any
from django.core.exceptions import ValidationError
import json

# Configurable operators - can be extended without code changes
ALLOWED_OPERATORS = {
    '==', '!=', '>', '>=', '<', '<=', 'in', 'not_in'
}

ALLOWED_LOGIC_OPERATORS = {'and', 'or'}

ALLOWED_OPERAND_KINDS = {'field', 'value'}


def validate_conditional_structure(json_value: Any) -> None:
    """
    Validates that a JSON value conforms to the conditional structure.

    Args:
        json_value: The value to validate (can be dict, str, or already parsed JSON)

    Raises:
        ValidationError: If validation fails (including invalid JSON strings)
    """
    # Parse JSON string if needed
    if isinstance(json_value, str):
        try:
            json_value = json.loads(json_value)
        except json.JSONDecodeError:
            raise ValidationError("Invalid JSON string")

    # Validate that it's a dictionary
    if not isinstance(json_value, dict):
        raise ValidationError("Conditional structure must be a dictionary")

    # Validate required keys
    if 'logic' not in json_value:
        raise ValidationError("Missing required key 'logic'")

    if 'conditions' not in json_value:
        raise ValidationError("Missing required key 'conditions'")

    # Validate logic operator
    logic = json_value['logic']
    if logic not in ALLOWED_LOGIC_OPERATORS:
        raise ValidationError(
            f"Invalid logic operator '{logic}'. Must be one of: {', '.join(ALLOWED_LOGIC_OPERATORS)}"
        )

    # Validate conditions is a list
    conditions = json_value['conditions']
    if not isinstance(conditions, list):
        raise ValidationError("'conditions' must be a list")

    # Validate conditions is not empty
    if len(conditions) == 0:
        raise ValidationError("'conditions' list cannot be empty")

    # Validate each condition
    for idx, condition in enumerate(conditions):
        _validate_condition(condition, idx)


def validate_operator(op: str) -> bool:
    """
    Validates that an operator is in the allowed set.

    Args:
        op: The operator string to validate

    Returns:
        True if operator is allowed, False otherwise
    """
    return op in ALLOWED_OPERATORS


def validate_operand(operand: Dict[str, Any]) -> None:
    """
    Validates that an operand (left or right) has correct structure.

    An operand must have exactly one key: either 'field' or 'value'.

    Args:
        operand: The operand dictionary to validate

    Raises:
        ValidationError: If validation fails
    """
    # Validate that operand is a dictionary
    if not isinstance(operand, dict):
        raise ValidationError("Operand must be a dictionary")

    # Validate that operand has exactly one key
    keys = list(operand.keys())
    if len(keys) == 0:
        raise ValidationError("Operand cannot be empty")

    if len(keys) > 1:
        raise ValidationError(
            f"Operand must have exactly one key, found: {', '.join(keys)}"
        )

    # Validate that the key is either 'field' or 'value'
    key = keys[0]
    if key not in ALLOWED_OPERAND_KINDS:
        raise ValidationError(
            f"Invalid operand key '{key}'. Must be one of: {', '.join(ALLOWED_OPERAND_KINDS)}"
        )

    # Validate field operand
    if key == 'field':
        field_value = operand[key]
        if not isinstance(field_value, str):
            raise ValidationError("Field operand value must be a string")
        if not field_value:
            raise ValidationError("Field operand value cannot be empty")

    # Value operand can be any JSON type, no additional validation needed


def _validate_condition(condition: Dict[str, Any], index: int) -> None:
    """
    Validates a single condition structure.

    Args:
        condition: The condition dictionary to validate
        index: The index of the condition in the conditions list (for error messages)

    Raises:
        ValidationError: If validation fails
    """
    # Validate that condition is a dictionary
    if not isinstance(condition, dict):
        raise ValidationError(f"Condition at index {index} must be a dictionary")

    # Validate required keys
    required_keys = {'left', 'op', 'right'}
    missing_keys = required_keys - set(condition.keys())
    if missing_keys:
        raise ValidationError(
            f"Condition at index {index} missing required keys: {', '.join(missing_keys)}"
        )

    # Validate operator
    op = condition['op']
    if not validate_operator(op):
        raise ValidationError(
            f"Condition at index {index} has invalid operator '{op}'. "
            f"Must be one of: {', '.join(ALLOWED_OPERATORS)}"
        )

    # Validate left operand
    validate_operand(condition['left'])

    # Validate right operand
    validate_operand(condition['right'])
