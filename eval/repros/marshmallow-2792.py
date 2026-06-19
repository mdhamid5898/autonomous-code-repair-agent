# marshmallow-2792.py — data_key not applied in schema validator ValidationErrors
#
# Bug: when @validates_schema raises ValidationError({'field_name': msg}), the key
# in the error dict uses the Python attribute name, NOT the external data_key.
# This is inconsistent with how marshmallow normally reports errors (using data_key
# as the external-facing key). The result is that errors end up under different
# keys depending on how they were raised.
#
# Fix: in schema.py::_run_validator, look up the field's .data_key after catching
# the ValidationError and use that key (falling back to the field name) when calling
# error_store.store_error(). This requires understanding the field objects defined
# in the schema's .fields dict (schema.py reads them; types.py defines SchemaValidator).
import pytest
import marshmallow
from marshmallow import fields, validates_schema, validates, ValidationError


class ApiSchema(marshmallow.Schema):
    """Field uses data_key so external name differs from Python attribute name."""

    ip_addresses = fields.String(required=True, data_key="ipAddresses")

    @validates_schema
    def validate_cross_field(self, data, **kwargs):
        # Raises with Python attribute name; should appear under external key
        if "ip_addresses" in data:
            raise ValidationError({"ip_addresses": "Schema validator error."})


def test_schema_validator_error_uses_data_key():
    """@validates_schema errors must use the external data_key, not the attr name.

    Before fix: the error appears under 'ip_addresses' (Python attr name).
    After fix:  the error appears under 'ipAddresses' (data_key / external name).
    """
    try:
        ApiSchema().load({"ipAddresses": "192.168.1.1"})
    except ValidationError as exc:
        messages = exc.messages
    else:
        pytest.fail("Expected ValidationError was not raised.")

    assert "ipAddresses" in messages, (
        f"Bug: schema validator error appeared under wrong key. "
        f"Got keys {list(messages.keys())!r}; expected 'ipAddresses' (the data_key). "
        f"The error is probably under 'ip_addresses' (the Python attribute name)."
    )
    assert "ip_addresses" not in messages, (
        f"Bug: schema validator error also appeared under the Python attr name "
        f"'ip_addresses' instead of/in addition to the data_key 'ipAddresses'. "
        f"messages={messages!r}"
    )
