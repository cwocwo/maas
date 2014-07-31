# Copyright 2014 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for `provisioningserver.drivers.power`."""

from __future__ import (
    absolute_import,
    print_function,
    unicode_literals,
    )

str = None

__metaclass__ = type
__all__ = []

from maastesting.factory import factory
from maastesting.testcase import MAASTestCase
from mock import sentinel
from provisioningserver.drivers import (
    make_setting_field,
    validate_settings,
    )
from provisioningserver.drivers.power import (
    get_error_message,
    PowerActionError,
    PowerAuthError,
    PowerConnError,
    PowerDriverBase,
    PowerDriverRegistry,
    PowerError,
    PowerSettingError,
    PowerToolError,
    )
from provisioningserver.utils.testing import RegistryFixture


class FakePowerDriverBase(PowerDriverBase):

    name = ""
    description = ""
    settings = []

    def __init__(self, name, description, settings):
        self.name = name
        self.description = description
        self.settings = settings
        super(FakePowerDriverBase, self).__init__()

    def on(self, system_id, **kwargs):
        raise NotImplementedError

    def off(self, system_id, **kwargs):
        raise NotImplementedError

    def query(self, system_id, **kwargs):
        raise NotImplementedError


def make_power_driver_base(name=None, description=None, settings=None):
    if name is None:
        name = factory.make_name('diskless')
    if description is None:
        description = factory.make_name('description')
    if settings is None:
        settings = []
    return FakePowerDriverBase(name, description, settings)


class TestFakePowerDriverBase(MAASTestCase):

    def test_attributes(self):
        fake_name = factory.make_name('name')
        fake_description = factory.make_name('description')
        fake_setting = factory.make_name('setting')
        fake_settings = [
            make_setting_field(
                fake_setting, fake_setting.title()),
            ]
        attributes = {
            'name': fake_name,
            'description': fake_description,
            'settings': fake_settings,
            }
        fake_driver = FakePowerDriverBase(
            fake_name, fake_description, fake_settings)
        self.assertAttributes(fake_driver, attributes)

    def test_make_power_driver_base(self):
        fake_name = factory.make_name('name')
        fake_description = factory.make_name('description')
        fake_setting = factory.make_name('setting')
        fake_settings = [
            make_setting_field(
                fake_setting, fake_setting.title()),
            ]
        attributes = {
            'name': fake_name,
            'description': fake_description,
            'settings': fake_settings,
            }
        fake_driver = make_power_driver_base(
            name=fake_name, description=fake_description,
            settings=fake_settings)
        self.assertAttributes(fake_driver, attributes)

    def test_make_power_driver_base_makes_name_and_description(self):
        fake_driver = make_power_driver_base()
        self.assertNotEqual("", fake_driver.name)
        self.assertNotEqual("", fake_driver.description)

    def test_on_raises_not_implemented(self):
        fake_driver = make_power_driver_base()
        self.assertRaises(
            NotImplementedError,
            fake_driver.on, sentinel.system_id)

    def test_off_raises_not_implemented(self):
        fake_driver = make_power_driver_base()
        self.assertRaises(
            NotImplementedError,
            fake_driver.off, sentinel.system_id)

    def test_query_raises_not_implemented(self):
        fake_driver = make_power_driver_base()
        self.assertRaises(
            NotImplementedError,
            fake_driver.query, sentinel.system_id)


class TestPowerDriverBase(MAASTestCase):

    def test_get_schema(self):
        fake_name = factory.make_name('name')
        fake_description = factory.make_name('description')
        fake_setting = factory.make_name('setting')
        fake_settings = [
            make_setting_field(
                fake_setting, fake_setting.title()),
            ]
        fake_driver = make_power_driver_base()
        self.assertItemsEqual({
            'name': fake_name,
            'description': fake_description,
            'fields': fake_settings,
            },
            fake_driver.get_schema())

    def test_get_schema_returns_valid_schema(self):
        fake_driver = make_power_driver_base()
        #: doesn't raise ValidationError
        validate_settings(fake_driver.get_schema())


class TestPowerDriverRegistry(MAASTestCase):

    def setUp(self):
        super(TestPowerDriverRegistry, self).setUp()
        # Ensure the global registry is empty for each test run.
        self.useFixture(RegistryFixture())

    def test_registry(self):
        self.assertItemsEqual([], PowerDriverRegistry)
        PowerDriverRegistry.register_item("driver", sentinel.driver)
        self.assertIn(
            sentinel.driver,
            (item for name, item in PowerDriverRegistry))

    def test_get_schema(self):
        fake_driver_one = make_power_driver_base()
        fake_driver_two = make_power_driver_base()
        PowerDriverRegistry.register_item(
            fake_driver_one.name, fake_driver_one)
        PowerDriverRegistry.register_item(
            fake_driver_two.name, fake_driver_two)
        self.assertItemsEqual([
            {
                'name': fake_driver_one.name,
                'description': fake_driver_one.description,
                'fields': [],
            },
            {
                'name': fake_driver_two.name,
                'description': fake_driver_two.description,
                'fields': [],
            }],
            PowerDriverRegistry.get_schema())


class TestGetErrorMessage(MAASTestCase):

    scenarios = [
        ('auth', dict(
            exception=PowerAuthError('auth'),
            message="Could not authenticate to node's BMC: auth",
            )),
        ('conn', dict(
            exception=PowerConnError('conn'),
            message="Could not contact node's BMC: conn",
            )),
        ('setting', dict(
            exception=PowerSettingError('setting'),
            message="Missing or invalid power setting: setting",
            )),
        ('tool', dict(
            exception=PowerToolError('tool'),
            message="Missing power tool: tool",
            )),
        ('action', dict(
            exception=PowerActionError('action'),
            message="Failed to complete power action: action",
            )),
        ('unknown', dict(
            exception=PowerError(),
            message="Failed talking to node's BMC for an unknown reason.",
            )),
    ]

    def test_return_msg(self):
        self.assertEqual(self.message, get_error_message(self.exception))
