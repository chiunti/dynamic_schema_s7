from django.utils.module_loading import import_string

DATATYPE_VALIDATORS = {
    'conditional': 'schemas.services.conditional_validator.validate_conditional_value',
}


def validate_datatype_value(data_type, value):
    """
    Validates a value against the validator registered for a datatype.
    No-op if no validator is registered or value is None.
    """
    if value is None:
        return

    validator_path = DATATYPE_VALIDATORS.get(data_type.name)
    if validator_path:
        validator = import_string(validator_path)
        validator(value)


def get_storage_type(data_type):
    """
    Returns the effective primary storage type for a DataType.
    Falls back to the datatype name for legacy datatypes that have not been migrated yet.
    """
    storage = data_type.primary_storage_type
    if storage:
        return storage

    name = data_type.name
    if name in ('string', 'date', 'color', 'internal', 'uuid', 'url', 'auto_uuid'):
        return 'string'
    if name in ('number', 'int', 'float', 'integer', 'decimal'):
        return 'number'
    if name in ('bool', 'boolean'):
        return 'bool'
    return 'json'


def get_storage_defaults(data_type, value):
    """
    Build a defaults dict for update_or_create_node_attribute based on the
    DataType's primary_storage_type.
    """
    storage = get_storage_type(data_type)

    if storage == 'string':
        return {"value_string": str(value), "value_number": None, "value_bool": None, "value_json": None}
    elif storage == 'number':
        return {"value_string": None, "value_number": value, "value_bool": None, "value_json": None}
    elif storage == 'bool':
        return {"value_string": None, "value_number": None, "value_bool": bool(value), "value_json": None}
    elif storage == 'json':
        return {"value_string": None, "value_number": None, "value_bool": None, "value_json": value}
    else:
        raise ValueError(f"Unsupported primary_storage_type: {storage}")
