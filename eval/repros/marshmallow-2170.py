# marshmallow-2170.py — schema validator raises ValidationError with wrong key
# Bug: when a @validates_schema method raises ValidationError(msg, field_name="foo")
# and field "foo" has a data_key set (e.g. data_key="fooKey"), the error ends up
# under "foo" (the attribute name) instead of "fooKey" (the data_key / serialization name).
import pytest
import marshmallow as ma


class MySchema(ma.Schema):
    ip_addresses = ma.fields.List(ma.fields.String(), data_key="ipAddresses")

    @ma.validates_schema
    def validate_all(self, data, **kwargs):
        raise ma.ValidationError("Custom error.", field_name="ip_addresses")


def test_schema_validator_uses_data_key_in_error():
    schema = MySchema()
    with pytest.raises(ma.ValidationError) as exc_info:
        schema.load({"ipAddresses": ["1.2.3.4"]})

    errors = exc_info.value.messages
    # Bug: error is reported under 'ip_addresses' (attribute name)
    # Expected: error should be under 'ipAddresses' (data_key, the serialized name)
    assert "ip_addresses" not in errors, (
        "Bug: ValidationError key is the attribute name 'ip_addresses' instead of "
        "the data_key 'ipAddresses'. errors=%r" % errors
    )
    assert "ipAddresses" in errors, (
        "Expected error under data_key 'ipAddresses', got: %r" % errors
    )
