# Copyright 2015 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Tests for `maasserver.websockets.listner`"""

from __future__ import (
    absolute_import,
    print_function,
    unicode_literals,
    )

str = None

__metaclass__ = type
__all__ = []

from collections import namedtuple

from crochet import wait_for_reactor
from django.contrib.auth.models import User
from django.db import connection
from maasserver.models.event import Event
from maasserver.models.node import Node
from maasserver.models.nodegroup import NodeGroup
from maasserver.models.nodegroupinterface import NodeGroupInterface
from maasserver.models.zone import Zone
from maasserver.testing.factory import factory
from maasserver.testing.testcase import MAASServerTestCase
from maasserver.triggers import register_all_triggers
from maasserver.utils.orm import transactional
from maasserver.websockets.listener import (
    PostgresListener,
    PostgresListenerNotifyError,
)
from maastesting.djangotestcase import DjangoTransactionTestCase
from maastesting.matchers import (
    MockCalledOnceWith,
    MockCalledWith,
)
from mock import (
    ANY,
    sentinel,
)
from provisioningserver.utils.twisted import DeferredValue
from psycopg2 import OperationalError
from twisted.internet import reactor
from twisted.internet.defer import inlineCallbacks
from twisted.internet.threads import deferToThread


FakeNotify = namedtuple("FakeNotify", ["channel", "payload"])


class TestPostgresListener(MAASServerTestCase):

    @transactional
    def send_notification(self, event, obj_id):
        cursor = connection.cursor()
        cursor.execute("NOTIFY %s, '%s';" % (event, obj_id))
        cursor.close()

    @wait_for_reactor
    @inlineCallbacks
    def test__calls_handler_on_notification(self):
        listener = PostgresListener()
        dv = DeferredValue()
        listener.register("node", lambda *args: dv.set(args))
        yield listener.start()
        try:
            yield deferToThread(self.send_notification, "node_create", 1)
            yield dv.get(timeout=2)
            self.assertEqual(('create', '1'), dv.value)
        finally:
            yield listener.stop()

    @wait_for_reactor
    @inlineCallbacks
    def test__tryConnection_connects_to_database(self):
        listener = PostgresListener()

        yield listener.tryConnection()
        try:
            self.assertTrue(listener.connected())
        finally:
            yield listener.stop()

    @wait_for_reactor
    @inlineCallbacks
    def test__tryConnection_logs_error(self):
        listener = PostgresListener()

        exc = factory.make_exception()
        self.patch(listener, "startConnection").side_effect = exc
        mock_logMsg = self.patch(listener, "logMsg")
        self.patch(reactor, "callLater")
        yield listener.tryConnection()
        self.assertThat(
            mock_logMsg,
            MockCalledOnceWith(
                format="Unable to connect to database: %(error)r",
                error=exc.message))

    @wait_for_reactor
    @inlineCallbacks
    def test__tryConnection_will_retry_in_3_seconds(self):
        listener = PostgresListener()

        self.patch(
            listener, "startConnection").side_effect = factory.make_exception()
        mock_callLater = self.patch(reactor, "callLater")
        yield listener.tryConnection()
        self.assertThat(
            mock_callLater,
            MockCalledWith(3, listener.tryConnection))

    @wait_for_reactor
    @inlineCallbacks
    def test__tryConnection_calls_registerChannels_after_startConnection(self):
        listener = PostgresListener()

        self.patch(listener, "startConnection")
        mock_registerChannels = self.patch(listener, "registerChannels")
        mock_registerChannels.side_effect = factory.make_exception()
        self.patch(reactor, "callLater")
        yield listener.tryConnection()
        self.assertThat(
            mock_registerChannels,
            MockCalledOnceWith())

    @wait_for_reactor
    @inlineCallbacks
    def test__tryConnection_adds_self_to_reactor(self):
        listener = PostgresListener()

        self.patch(listener, "startConnection")
        self.patch(listener, "registerChannels")
        mock_addReader = self.patch(reactor, "addReader")
        yield listener.tryConnection()
        self.assertThat(
            mock_addReader,
            MockCalledOnceWith(listener))

    @wait_for_reactor
    @inlineCallbacks
    def test__tryConnection_logs_success(self):
        listener = PostgresListener()

        mock_logMsg = self.patch(listener, "logMsg")
        yield listener.tryConnection()
        try:
            self.assertThat(
                mock_logMsg,
                MockCalledOnceWith("Listening for notificaton from database."))
        finally:
            yield listener.stop()

    def test_register_adds_channel_and_handler(self):
        listener = PostgresListener()
        channel = factory.make_name("channel")
        listener.register(channel, sentinel.handler)
        self.assertEqual(
            [sentinel.handler], listener.listeners[channel])

    def test__convertChannel_raises_exception_if_not_valid_channel(self):
        listener = PostgresListener()
        self.assertRaises(
            PostgresListenerNotifyError,
            listener.convertChannel, "node_create")

    def test__convertChannel_raises_exception_if_not_valid_action(self):
        listener = PostgresListener()
        self.assertRaises(
            PostgresListenerNotifyError,
            listener.convertChannel, "node_unknown")

    def test__doRead_removes_self_from_reactor_on_error(self):
        listener = PostgresListener()

        connection = self.patch(listener, "connection")
        connection.connection.poll.side_effect = OperationalError()

        self.patch(reactor, "removeReader")
        self.patch(listener, "connectionLost")
        listener.doRead()
        self.assertThat(
            reactor.removeReader,
            MockCalledOnceWith(listener))
        self.assertThat(
            listener.connectionLost,
            MockCalledOnceWith(ANY))

    def test__doRead_adds_notifies_to_notifications(self):
        listener = PostgresListener()
        notifications = [
            FakeNotify(
                channel=factory.make_name("channel_action"),
                payload=factory.make_name("payload"))
            for _ in range(3)
            ]

        connection = self.patch(listener, "connection")
        connection.connection.poll.return_value = None
        # Add the notifications twice, so it can test that duplicates are
        # accumulated together.
        connection.connection.notifies = notifications + notifications
        self.patch(listener, "handleNotify")

        listener.doRead()
        self.assertItemsEqual(
            listener.notifications, set(notifications))


class TransactionalHelpersMixin:
    """Helpers performing actions in transactions."""

    def make_listener_without_delay(self):
        listener = PostgresListener()
        self.patch(listener, "HANDLE_NOTIFY_DELAY", 0)
        return listener

    @transactional
    def create_node(self, params=None):
        if params is None:
            params = {}
        return factory.make_Node(**params)

    @transactional
    def update_node(self, system_id, params):
        node = Node.objects.get(system_id=system_id)
        for key, value in params.items():
            setattr(node, key, value)
        return node.save()

    @transactional
    def delete_node(self, system_id):
        node = Node.objects.get(system_id=system_id)
        node.delete()

    @transactional
    def create_nodegroup(self, params=None):
        if params is None:
            params = {}
        return factory.make_NodeGroup(**params)

    @transactional
    def update_nodegroup(self, id, params):
        nodegroup = NodeGroup.objects.get(id=id)
        for key, value in params.items():
            setattr(nodegroup, key, value)
        return nodegroup.save()

    @transactional
    def delete_nodegroup(self, id):
        nodegroup = NodeGroup.objects.get(id=id)
        nodegroup.delete()

    @transactional
    def create_nodegroupinterface(self, nodegroup, params=None):
        if params is None:
            params = {}
        return factory.make_NodeGroupInterface(nodegroup, **params)

    @transactional
    def update_nodegroupinterface(self, id, params):
        interface = NodeGroupInterface.objects.get(id=id)
        for key, value in params.items():
            setattr(interface, key, value)
        return interface.save()

    @transactional
    def delete_nodegroupinterface(self, id):
        interface = NodeGroupInterface.objects.get(id=id)
        interface.delete()

    @transactional
    def create_zone(self, params=None):
        if params is None:
            params = {}
        return factory.make_Zone(**params)

    @transactional
    def update_zone(self, id, params):
        zone = Zone.objects.get(id=id)
        for key, value in params.items():
            setattr(zone, key, value)
        return zone.save()

    @transactional
    def delete_zone(self, id):
        zone = Zone.objects.get(id=id)
        zone.delete()

    @transactional
    def create_tag(self, params=None):
        if params is None:
            params = {}
        return factory.make_Tag(**params)

    @transactional
    def add_node_to_tag(self, node, tag):
        node.tags.add(tag)
        node.save()

    @transactional
    def remove_node_from_tag(self, node, tag):
        node.tags.remove(tag)
        node.save()

    @transactional
    def update_tag(self, tag, new_name):
        tag.name = new_name
        tag.save()
        return tag

    @transactional
    def create_user(self, params=None):
        if params is None:
            params = {}
        return factory.make_User(**params)

    @transactional
    def update_user(self, id, params):
        user = User.objects.get(id=id)
        for key, value in params.items():
            setattr(user, key, value)
        return user.save()

    @transactional
    def delete_user(self, id):
        user = User.objects.get(id=id)
        user.consumers.all().delete()
        user.delete()

    @transactional
    def create_event(self, params=None):
        if params is None:
            params = {}
        return factory.make_Event(**params)

    @transactional
    def update_event(self, id, params):
        event = Event.objects.get(id=id)
        for key, value in params.items():
            setattr(event, key, value)
        return event.save()

    @transactional
    def delete_event(self, id):
        event = Event.objects.get(id=id)
        event.delete()


class TestNodeListener(DjangoTransactionTestCase, TransactionalHelpersMixin):
    """End-to-end test of both the listeners code and the triggers code."""

    scenarios = (
        ('node', {
            'params': {'installable': True},
            'listener': 'node',
            }),
        ('device', {
            'params': {'installable': False},
            'listener': 'device',
            }),
    )

    @wait_for_reactor
    @inlineCallbacks
    def test__calls_handler_on_create_notification(self):
        yield deferToThread(register_all_triggers)
        listener = self.make_listener_without_delay()
        dv = DeferredValue()
        listener.register(self.listener, lambda *args: dv.set(args))
        yield listener.start()
        try:
            node = yield deferToThread(self.create_node, self.params)
            yield dv.get(timeout=2)
            self.assertEqual(('create', node.system_id), dv.value)
        finally:
            yield listener.stop()

    @wait_for_reactor
    @inlineCallbacks
    def test__calls_handler_on_update_notification(self):
        yield deferToThread(register_all_triggers)
        listener = self.make_listener_without_delay()
        dv = DeferredValue()
        listener.register(self.listener, lambda *args: dv.set(args))
        node = yield deferToThread(self.create_node, self.params)

        yield listener.start()
        try:
            yield deferToThread(
                self.update_node,
                node.system_id,
                {'hostname': factory.make_name('hostname')})
            yield dv.get(timeout=2)
            self.assertEqual(('update', node.system_id), dv.value)
        finally:
            yield listener.stop()

    @wait_for_reactor
    @inlineCallbacks
    def test__calls_handler_on_delete_notification(self):
        yield deferToThread(register_all_triggers)
        listener = self.make_listener_without_delay()
        dv = DeferredValue()
        listener.register(self.listener, lambda *args: dv.set(args))
        node = yield deferToThread(self.create_node, self.params)
        yield listener.start()
        try:
            yield deferToThread(self.delete_node, node.system_id)
            yield dv.get(timeout=2)
            self.assertEqual(('delete', node.system_id), dv.value)
        finally:
            yield listener.stop()


class TestClusterListener(
        DjangoTransactionTestCase, TransactionalHelpersMixin):
    """End-to-end test of both the listeners code and the cluster
    triggers code."""

    @wait_for_reactor
    @inlineCallbacks
    def test__calls_handler_on_create_notification(self):
        yield deferToThread(register_all_triggers)
        listener = self.make_listener_without_delay()
        dv = DeferredValue()
        listener.register("nodegroup", lambda *args: dv.set(args))
        yield listener.start()
        try:
            nodegroup = yield deferToThread(self.create_nodegroup)
            yield dv.get(timeout=2)
            self.assertEqual(('create', '%s' % nodegroup.id), dv.value)
        finally:
            yield listener.stop()

    @wait_for_reactor
    @inlineCallbacks
    def test__calls_handler_on_update_notification(self):
        yield deferToThread(register_all_triggers)
        listener = self.make_listener_without_delay()
        dv = DeferredValue()
        listener.register("nodegroup", lambda *args: dv.set(args))
        nodegroup = yield deferToThread(self.create_nodegroup)

        yield listener.start()
        try:
            yield deferToThread(
                self.update_nodegroup,
                nodegroup.id,
                {'cluster_name': factory.make_name('cluster_name')})
            yield dv.get(timeout=2)
            self.assertEqual(('update', '%s' % nodegroup.id), dv.value)
        finally:
            yield listener.stop()

    @wait_for_reactor
    @inlineCallbacks
    def test__calls_handler_on_delete_notification(self):
        yield deferToThread(register_all_triggers)
        listener = self.make_listener_without_delay()
        dv = DeferredValue()
        listener.register("nodegroup", lambda *args: dv.set(args))
        nodegroup = yield deferToThread(self.create_nodegroup)
        yield listener.start()
        try:
            yield deferToThread(self.delete_nodegroup, nodegroup.id)
            yield dv.get(timeout=2)
            self.assertEqual(('delete', '%s' % nodegroup.id), dv.value)
        finally:
            yield listener.stop()


class TestClusterInterfaceListener(
        DjangoTransactionTestCase, TransactionalHelpersMixin):
    """End-to-end test of both the listeners code and the cluster interface
    triggers code."""

    @wait_for_reactor
    @inlineCallbacks
    def test__calls_nodegroup_update_handler_on_create_notification(self):
        yield deferToThread(register_all_triggers)
        nodegroup = yield deferToThread(self.create_nodegroup)

        listener = self.make_listener_without_delay()
        dv = DeferredValue()
        listener.register("nodegroup", lambda *args: dv.set(args))
        yield listener.start()
        try:
            yield deferToThread(
                self.create_nodegroupinterface, nodegroup)
            yield dv.get(timeout=2)
            self.assertEqual(('update', '%s' % nodegroup.id), dv.value)
        finally:
            yield listener.stop()

    @wait_for_reactor
    @inlineCallbacks
    def test__calls_nodegroup_update_handler_on_update_notification(self):
        yield deferToThread(register_all_triggers)
        nodegroup = yield deferToThread(self.create_nodegroup)
        interface = yield deferToThread(
            self.create_nodegroupinterface, nodegroup)

        listener = self.make_listener_without_delay()
        dv = DeferredValue()
        listener.register("nodegroup", lambda *args: dv.set(args))
        yield listener.start()
        try:
            yield deferToThread(
                self.update_nodegroupinterface,
                interface.id,
                {'name': factory.make_name('name')})
            yield dv.get(timeout=2)
            self.assertEqual(('update', '%s' % nodegroup.id), dv.value)
        finally:
            yield listener.stop()

    @wait_for_reactor
    @inlineCallbacks
    def test__calls_nodegroup_update_handler_on_delete_notification(self):
        yield deferToThread(register_all_triggers)
        nodegroup = yield deferToThread(self.create_nodegroup)
        interface = yield deferToThread(
            self.create_nodegroupinterface, nodegroup)

        listener = self.make_listener_without_delay()
        dv = DeferredValue()
        listener.register("nodegroup", lambda *args: dv.set(args))
        yield listener.start()
        try:
            yield deferToThread(self.delete_nodegroupinterface, interface.id)
            yield dv.get(timeout=2)
            self.assertEqual(('update', '%s' % nodegroup.id), dv.value)
        finally:
            yield listener.stop()


class TestZoneListener(DjangoTransactionTestCase, TransactionalHelpersMixin):
    """End-to-end test of both the listeners code and the zone
    triggers code."""

    @wait_for_reactor
    @inlineCallbacks
    def test__calls_handler_on_create_notification(self):
        yield deferToThread(register_all_triggers)
        listener = self.make_listener_without_delay()
        dv = DeferredValue()
        listener.register("zone", lambda *args: dv.set(args))
        yield listener.start()
        try:
            zone = yield deferToThread(self.create_zone)
            yield dv.get(timeout=2)
            self.assertEqual(('create', '%s' % zone.id), dv.value)
        finally:
            yield listener.stop()

    @wait_for_reactor
    @inlineCallbacks
    def test__calls_handler_on_update_notification(self):
        yield deferToThread(register_all_triggers)
        listener = self.make_listener_without_delay()
        dv = DeferredValue()
        listener.register("zone", lambda *args: dv.set(args))
        zone = yield deferToThread(self.create_zone)

        yield listener.start()
        try:
            yield deferToThread(
                self.update_zone,
                zone.id,
                {'cluster_name': factory.make_name('cluster_name')})
            yield dv.get(timeout=2)
            self.assertEqual(('update', '%s' % zone.id), dv.value)
        finally:
            yield listener.stop()

    @wait_for_reactor
    @inlineCallbacks
    def test__calls_handler_on_delete_notification(self):
        yield deferToThread(register_all_triggers)
        listener = self.make_listener_without_delay()
        dv = DeferredValue()
        listener.register("zone", lambda *args: dv.set(args))
        zone = yield deferToThread(self.create_zone)
        yield listener.start()
        try:
            yield deferToThread(self.delete_zone, zone.id)
            yield dv.get(timeout=2)
            self.assertEqual(('delete', '%s' % zone.id), dv.value)
        finally:
            yield listener.stop()


class TestNodeTagListener(
        DjangoTransactionTestCase, TransactionalHelpersMixin):
    """End-to-end test of both the listeners code and the triggers on
    maasserver_node_tags table."""

    scenarios = (
        ('node', {
            'params': {'installable': True},
            'listener': 'node',
            }),
        ('device', {
            'params': {'installable': False},
            'listener': 'device',
            }),
    )

    @wait_for_reactor
    @inlineCallbacks
    def test__calls_handler_with_update_on_create(self):
        yield deferToThread(register_all_triggers)
        node = yield deferToThread(self.create_node, self.params)
        tag = yield deferToThread(self.create_tag)

        listener = self.make_listener_without_delay()
        dv = DeferredValue()
        listener.register(self.listener, lambda *args: dv.set(args))
        yield listener.start()
        try:
            yield deferToThread(self.add_node_to_tag, node, tag)
            yield dv.get(timeout=2)
            self.assertEqual(('update', '%s' % node.system_id), dv.value)
        finally:
            yield listener.stop()

    @wait_for_reactor
    @inlineCallbacks
    def test__calls_handler_with_update_on_delete(self):
        yield deferToThread(register_all_triggers)
        node = yield deferToThread(self.create_node, self.params)
        tag = yield deferToThread(self.create_tag)

        listener = self.make_listener_without_delay()
        dv = DeferredValue()
        listener.register(self.listener, lambda *args: dv.set(args))
        yield listener.start()
        try:
            yield deferToThread(self.remove_node_from_tag, node, tag)
            yield dv.get(timeout=2)
            self.assertEqual(('update', '%s' % node.system_id), dv.value)
        finally:
            yield listener.stop()

    @wait_for_reactor
    @inlineCallbacks
    def test__calls_node_handler_with_update_on_tag_rename(self):
        yield deferToThread(register_all_triggers)
        node = yield deferToThread(self.create_node, self.params)
        tag = yield deferToThread(self.create_tag)
        yield deferToThread(self.add_node_to_tag, node, tag)

        listener = self.make_listener_without_delay()
        dv = DeferredValue()
        listener.register(self.listener, lambda *args: dv.set(args))
        yield listener.start()
        try:
            tag = yield deferToThread(
                self.update_tag, tag, factory.make_name("tag"))
            yield dv.get(timeout=2)
            self.assertEqual(('update', '%s' % node.system_id), dv.value)
        finally:
            yield listener.stop()


class TestUserListener(DjangoTransactionTestCase, TransactionalHelpersMixin):
    """End-to-end test of both the listeners code and the user
    triggers code."""

    @wait_for_reactor
    @inlineCallbacks
    def test__calls_handler_on_create_notification(self):
        yield deferToThread(register_all_triggers)
        listener = self.make_listener_without_delay()
        dv = DeferredValue()
        listener.register("user", lambda *args: dv.set(args))
        yield listener.start()
        try:
            user = yield deferToThread(self.create_user)
            yield dv.get(timeout=2)
            self.assertEqual(('create', '%s' % user.id), dv.value)
        finally:
            yield listener.stop()

    @wait_for_reactor
    @inlineCallbacks
    def test__calls_handler_on_update_notification(self):
        yield deferToThread(register_all_triggers)
        listener = self.make_listener_without_delay()
        dv = DeferredValue()
        listener.register("user", lambda *args: dv.set(args))
        user = yield deferToThread(self.create_user)

        yield listener.start()
        try:
            yield deferToThread(
                self.update_user,
                user.id,
                {'username': factory.make_name('username')})
            yield dv.get(timeout=2)
            self.assertEqual(('update', '%s' % user.id), dv.value)
        finally:
            yield listener.stop()

    @wait_for_reactor
    @inlineCallbacks
    def test__calls_handler_on_delete_notification(self):
        yield deferToThread(register_all_triggers)
        listener = self.make_listener_without_delay()
        dv = DeferredValue()
        listener.register("user", lambda *args: dv.set(args))
        user = yield deferToThread(self.create_user)
        yield listener.start()
        try:
            yield deferToThread(self.delete_user, user.id)
            yield dv.get(timeout=2)
            self.assertEqual(('delete', '%s' % user.id), dv.value)
        finally:
            yield listener.stop()


class TestEventListener(DjangoTransactionTestCase, TransactionalHelpersMixin):
    """End-to-end test of both the listeners code and the event
    triggers code."""

    @wait_for_reactor
    @inlineCallbacks
    def test__calls_handler_on_create_notification(self):
        yield deferToThread(register_all_triggers)
        listener = self.make_listener_without_delay()
        dv = DeferredValue()
        listener.register("event", lambda *args: dv.set(args))
        yield listener.start()
        try:
            event = yield deferToThread(self.create_event)
            yield dv.get(timeout=2)
            self.assertEqual(('create', '%s' % event.id), dv.value)
        finally:
            yield listener.stop()

    @wait_for_reactor
    @inlineCallbacks
    def test__calls_handler_on_update_notification(self):
        yield deferToThread(register_all_triggers)
        listener = self.make_listener_without_delay()
        dv = DeferredValue()
        listener.register("event", lambda *args: dv.set(args))
        event = yield deferToThread(self.create_event)

        yield listener.start()
        try:
            yield deferToThread(
                self.update_event,
                event.id,
                {'description': factory.make_name('description')})
            yield dv.get(timeout=2)
            self.assertEqual(('update', '%s' % event.id), dv.value)
        finally:
            yield listener.stop()

    @wait_for_reactor
    @inlineCallbacks
    def test__calls_handler_on_delete_notification(self):
        yield deferToThread(register_all_triggers)
        listener = self.make_listener_without_delay()
        dv = DeferredValue()
        listener.register("event", lambda *args: dv.set(args))
        event = yield deferToThread(self.create_event)
        yield listener.start()
        try:
            yield deferToThread(self.delete_event, event.id)
            yield dv.get(timeout=2)
            self.assertEqual(('delete', '%s' % event.id), dv.value)
        finally:
            yield listener.stop()


class TestNodeEventListener(
        DjangoTransactionTestCase, TransactionalHelpersMixin):
    """End-to-end test of both the listeners code and the triggers on
    maasserver_event table that notifies its node."""

    scenarios = (
        ('node', {
            'params': {'installable': True},
            'listener': 'node',
            }),
        ('device', {
            'params': {'installable': False},
            'listener': 'device',
            }),
    )

    @wait_for_reactor
    @inlineCallbacks
    def test__calls_handler_with_update_on_create(self):
        yield deferToThread(register_all_triggers)
        node = yield deferToThread(self.create_node, self.params)

        listener = self.make_listener_without_delay()
        dv = DeferredValue()
        listener.register(self.listener, lambda *args: dv.set(args))
        yield listener.start()
        try:
            yield deferToThread(self.create_event, {"node": node})
            yield dv.get(timeout=2)
            self.assertEqual(('update', '%s' % node.system_id), dv.value)
        finally:
            yield listener.stop()
