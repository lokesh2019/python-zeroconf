#!/usr/bin/env python
# -*- coding: utf-8 -*-


""" Unit tests for zeroconf.py """

import logging
import socket
import time
import unittest
import unittest.mock
from typing import Dict, Optional  # noqa # used in type hints

import pytest

import zeroconf as r
from zeroconf import ServiceBrowser, ServiceInfo, Zeroconf, const

from . import _clear_cache

log = logging.getLogger('zeroconf')
original_logging_level = logging.NOTSET


def setup_module():
    global original_logging_level
    original_logging_level = log.level
    log.setLevel(logging.DEBUG)


def teardown_module():
    if original_logging_level != logging.NOTSET:
        log.setLevel(original_logging_level)


class Names(unittest.TestCase):
    def test_long_name(self):
        generated = r.DNSOutgoing(const._FLAGS_QR_RESPONSE)
        question = r.DNSQuestion(
            "this.is.a.very.long.name.with.lots.of.parts.in.it.local.", const._TYPE_SRV, const._CLASS_IN
        )
        generated.add_question(question)
        r.DNSIncoming(generated.packets()[0])

    def test_exceedingly_long_name(self):
        generated = r.DNSOutgoing(const._FLAGS_QR_RESPONSE)
        name = "%slocal." % ("part." * 1000)
        question = r.DNSQuestion(name, const._TYPE_SRV, const._CLASS_IN)
        generated.add_question(question)
        r.DNSIncoming(generated.packets()[0])

    def test_extra_exceedingly_long_name(self):
        generated = r.DNSOutgoing(const._FLAGS_QR_RESPONSE)
        name = "%slocal." % ("part." * 4000)
        question = r.DNSQuestion(name, const._TYPE_SRV, const._CLASS_IN)
        generated.add_question(question)
        r.DNSIncoming(generated.packets()[0])

    def test_exceedingly_long_name_part(self):
        name = "%s.local." % ("a" * 1000)
        generated = r.DNSOutgoing(const._FLAGS_QR_RESPONSE)
        question = r.DNSQuestion(name, const._TYPE_SRV, const._CLASS_IN)
        generated.add_question(question)
        self.assertRaises(r.NamePartTooLongException, generated.packets)

    def test_same_name(self):
        name = "paired.local."
        generated = r.DNSOutgoing(const._FLAGS_QR_RESPONSE)
        question = r.DNSQuestion(name, const._TYPE_SRV, const._CLASS_IN)
        generated.add_question(question)
        generated.add_question(question)
        r.DNSIncoming(generated.packets()[0])

    def test_lots_of_names(self):

        # instantiate a zeroconf instance
        zc = Zeroconf(interfaces=['127.0.0.1'])

        # create a bunch of servers
        type_ = "_my-service._tcp.local."
        name = 'a wonderful service'
        server_count = 300
        self.generate_many_hosts(zc, type_, name, server_count)

        # verify that name changing works
        self.verify_name_change(zc, type_, name, server_count)

        # we are going to monkey patch the zeroconf send to check packet sizes
        old_send = zc.send

        longest_packet_len = 0
        longest_packet = None  # type: Optional[r.DNSOutgoing]

        def send(out, addr=const._MDNS_ADDR, port=const._MDNS_PORT):
            """Sends an outgoing packet."""
            for packet in out.packets():
                nonlocal longest_packet_len, longest_packet
                if longest_packet_len < len(packet):
                    longest_packet_len = len(packet)
                    longest_packet = out
                old_send(out, addr=addr, port=port)

        # monkey patch the zeroconf send
        setattr(zc, "send", send)

        # dummy service callback
        def on_service_state_change(zeroconf, service_type, state_change, name):
            pass

        # start a browser
        browser = ServiceBrowser(zc, type_, [on_service_state_change])

        # wait until the browse request packet has maxed out in size
        sleep_count = 0
        # we will never get to this large of a packet given the application-layer
        # splitting of packets, but we still want to track the longest_packet_len
        # for the debug message below
        while sleep_count < 100 and longest_packet_len < const._MAX_MSG_ABSOLUTE - 100:
            sleep_count += 1
            time.sleep(0.1)

        browser.cancel()
        time.sleep(0.5)

        import zeroconf

        zeroconf.log.debug('sleep_count %d, sized %d', sleep_count, longest_packet_len)

        # now the browser has sent at least one request, verify the size
        assert longest_packet_len <= const._MAX_MSG_TYPICAL
        assert longest_packet_len >= const._MAX_MSG_TYPICAL - 100

        # mock zeroconf's logger warning() and debug()
        from unittest.mock import patch

        patch_warn = patch('zeroconf._logger.log.warning')
        patch_debug = patch('zeroconf._logger.log.debug')
        mocked_log_warn = patch_warn.start()
        mocked_log_debug = patch_debug.start()

        # now that we have a long packet in our possession, let's verify the
        # exception handling.
        out = longest_packet
        assert out is not None
        out.data.append(b'\0' * 1000)

        # mock the zeroconf logger and check for the correct logging backoff
        call_counts = mocked_log_warn.call_count, mocked_log_debug.call_count
        # try to send an oversized packet
        zc.send(out)
        assert mocked_log_warn.call_count == call_counts[0]
        zc.send(out)
        assert mocked_log_warn.call_count == call_counts[0]

        # force a receive of a packet
        packet = out.packets()[0]
        s = zc._respond_sockets[0]

        # mock the zeroconf logger and check for the correct logging backoff
        call_counts = mocked_log_warn.call_count, mocked_log_debug.call_count
        # force receive on oversized packet
        s.sendto(packet, 0, (const._MDNS_ADDR, const._MDNS_PORT))
        s.sendto(packet, 0, (const._MDNS_ADDR, const._MDNS_PORT))
        time.sleep(2.0)
        zeroconf.log.debug(
            'warn %d debug %d was %s', mocked_log_warn.call_count, mocked_log_debug.call_count, call_counts
        )
        assert mocked_log_debug.call_count > call_counts[0]

        # close our zeroconf which will close the sockets
        zc.close()

        # pop the big chunk off the end of the data and send on a closed socket
        out.data.pop()
        zc._GLOBAL_DONE = False

        # mock the zeroconf logger and check for the correct logging backoff
        call_counts = mocked_log_warn.call_count, mocked_log_debug.call_count
        # send on a closed socket (force a socket error)
        zc.send(out)
        zeroconf.log.debug(
            'warn %d debug %d was %s', mocked_log_warn.call_count, mocked_log_debug.call_count, call_counts
        )
        assert mocked_log_warn.call_count > call_counts[0]
        assert mocked_log_debug.call_count > call_counts[0]
        zc.send(out)
        zeroconf.log.debug(
            'warn %d debug %d was %s', mocked_log_warn.call_count, mocked_log_debug.call_count, call_counts
        )
        assert mocked_log_debug.call_count > call_counts[0] + 2

        mocked_log_warn.stop()
        mocked_log_debug.stop()

    def verify_name_change(self, zc, type_, name, number_hosts):
        desc = {'path': '/~paulsm/'}
        info_service = ServiceInfo(
            type_,
            '%s.%s' % (name, type_),
            80,
            0,
            0,
            desc,
            "ash-2.local.",
            addresses=[socket.inet_aton("10.0.1.2")],
        )

        # verify name conflict
        self.assertRaises(r.NonUniqueNameException, zc.register_service, info_service)

        # verify no name conflict https://tools.ietf.org/html/rfc6762#section-6.6
        zc.register_service(info_service, cooperating_responders=True)

        zc.register_service(info_service, allow_name_change=True)
        assert info_service.name.split('.')[0] == '%s-%d' % (name, number_hosts + 1)

    def generate_many_hosts(self, zc, type_, name, number_hosts):
        records_per_server = 2
        block_size = 25
        number_hosts = int(((number_hosts - 1) / block_size + 1)) * block_size
        for i in range(1, number_hosts + 1):
            next_name = name if i == 1 else '%s-%d' % (name, i)
            self.generate_host(zc, next_name, type_)
            if i % block_size == 0:
                sleep_count = 0
                while sleep_count < 40 and i * records_per_server > len(zc.cache.entries_with_name(type_)):
                    sleep_count += 1
                    time.sleep(0.05)

    @staticmethod
    def generate_host(zc, host_name, type_):
        name = '.'.join((host_name, type_))
        out = r.DNSOutgoing(const._FLAGS_QR_RESPONSE | const._FLAGS_AA)
        out.add_answer_at_time(
            r.DNSPointer(type_, const._TYPE_PTR, const._CLASS_IN, const._DNS_OTHER_TTL, name), 0
        )
        out.add_answer_at_time(
            r.DNSService(
                type_,
                const._TYPE_SRV,
                const._CLASS_IN | const._CLASS_UNIQUE,
                const._DNS_HOST_TTL,
                0,
                0,
                80,
                name,
            ),
            0,
        )
        zc.send(out)


class TestRegistrar(unittest.TestCase):
    def test_ttl(self):

        # instantiate a zeroconf instance
        zc = Zeroconf(interfaces=['127.0.0.1'])

        # service definition
        type_ = "_test-srvc-type._tcp.local."
        name = "xxxyyy"
        registration_name = "%s.%s" % (name, type_)

        desc = {'path': '/~paulsm/'}
        info = ServiceInfo(
            type_,
            registration_name,
            80,
            0,
            0,
            desc,
            "ash-2.local.",
            addresses=[socket.inet_aton("10.0.1.2")],
        )

        nbr_answers = nbr_additionals = nbr_authorities = 0

        def get_ttl(record_type):
            if expected_ttl is not None:
                return expected_ttl
            elif record_type in [const._TYPE_A, const._TYPE_SRV]:
                return const._DNS_HOST_TTL
            else:
                return const._DNS_OTHER_TTL

        def _process_outgoing_packet(out):
            """Sends an outgoing packet."""
            nonlocal nbr_answers, nbr_additionals, nbr_authorities

            for answer, time_ in out.answers:
                nbr_answers += 1
                assert answer.ttl == get_ttl(answer.type)
            for answer in out.additionals:
                nbr_additionals += 1
                assert answer.ttl == get_ttl(answer.type)
            for answer in out.authorities:
                nbr_authorities += 1
                assert answer.ttl == get_ttl(answer.type)

        # register service with default TTL
        expected_ttl = None
        for _ in range(3):
            _process_outgoing_packet(zc.generate_service_query(info))
        zc.registry.add(info)
        for _ in range(3):
            _process_outgoing_packet(zc.generate_service_broadcast(info, None))
        assert nbr_answers == 12 and nbr_additionals == 0 and nbr_authorities == 3
        nbr_answers = nbr_additionals = nbr_authorities = 0

        # query
        query = r.DNSOutgoing(const._FLAGS_QR_QUERY | const._FLAGS_AA)
        assert query.is_query() is True
        query.add_question(r.DNSQuestion(info.type, const._TYPE_PTR, const._CLASS_IN))
        query.add_question(r.DNSQuestion(info.name, const._TYPE_SRV, const._CLASS_IN))
        query.add_question(r.DNSQuestion(info.name, const._TYPE_TXT, const._CLASS_IN))
        query.add_question(r.DNSQuestion(info.server, const._TYPE_A, const._CLASS_IN))
        _process_outgoing_packet(zc.query_handler.response(r.DNSIncoming(query.packets()[0]), False))
        assert nbr_answers == 4 and nbr_additionals == 4 and nbr_authorities == 0
        nbr_answers = nbr_additionals = nbr_authorities = 0

        # unregister
        expected_ttl = 0
        zc.registry.remove(info)
        for _ in range(3):
            _process_outgoing_packet(zc.generate_service_broadcast(info, 0))
        assert nbr_answers == 12 and nbr_additionals == 0 and nbr_authorities == 0
        nbr_answers = nbr_additionals = nbr_authorities = 0

        expected_ttl = None
        for _ in range(3):
            _process_outgoing_packet(zc.generate_service_query(info))
        zc.registry.add(info)
        # register service with custom TTL
        expected_ttl = const._DNS_HOST_TTL * 2
        assert expected_ttl != const._DNS_HOST_TTL
        for _ in range(3):
            _process_outgoing_packet(zc.generate_service_broadcast(info, expected_ttl))
        assert nbr_answers == 12 and nbr_additionals == 0 and nbr_authorities == 3
        nbr_answers = nbr_additionals = nbr_authorities = 0

        # query
        expected_ttl = None
        query = r.DNSOutgoing(const._FLAGS_QR_QUERY | const._FLAGS_AA)
        query.add_question(r.DNSQuestion(info.type, const._TYPE_PTR, const._CLASS_IN))
        query.add_question(r.DNSQuestion(info.name, const._TYPE_SRV, const._CLASS_IN))
        query.add_question(r.DNSQuestion(info.name, const._TYPE_TXT, const._CLASS_IN))
        query.add_question(r.DNSQuestion(info.server, const._TYPE_A, const._CLASS_IN))
        _process_outgoing_packet(zc.query_handler.response(r.DNSIncoming(query.packets()[0]), False))
        assert nbr_answers == 4 and nbr_additionals == 4 and nbr_authorities == 0
        nbr_answers = nbr_additionals = nbr_authorities = 0

        # unregister
        expected_ttl = 0
        zc.registry.remove(info)
        for _ in range(3):
            _process_outgoing_packet(zc.generate_service_broadcast(info, 0))
        assert nbr_answers == 12 and nbr_additionals == 0 and nbr_authorities == 0
        nbr_answers = nbr_additionals = nbr_authorities = 0
        zc.close()

    def test_name_conflicts(self):
        # instantiate a zeroconf instance
        zc = Zeroconf(interfaces=['127.0.0.1'])
        type_ = "_homeassistant._tcp.local."
        name = "Home"
        registration_name = "%s.%s" % (name, type_)

        info = ServiceInfo(
            type_,
            name=registration_name,
            server="random123.local.",
            addresses=[socket.inet_pton(socket.AF_INET, "1.2.3.4")],
            port=80,
            properties={"version": "1.0"},
        )
        zc.register_service(info)

        conflicting_info = ServiceInfo(
            type_,
            name=registration_name,
            server="random456.local.",
            addresses=[socket.inet_pton(socket.AF_INET, "4.5.6.7")],
            port=80,
            properties={"version": "1.0"},
        )
        with pytest.raises(r.NonUniqueNameException):
            zc.register_service(conflicting_info)
        zc.close()

    def test_register_and_lookup_type_by_uppercase_name(self):
        # instantiate a zeroconf instance
        zc = Zeroconf(interfaces=['127.0.0.1'])
        type_ = "_mylowertype._tcp.local."
        name = "Home"
        registration_name = "%s.%s" % (name, type_)

        info = ServiceInfo(
            type_,
            name=registration_name,
            server="random123.local.",
            addresses=[socket.inet_pton(socket.AF_INET, "1.2.3.4")],
            port=80,
            properties={"version": "1.0"},
        )
        zc.register_service(info)
        _clear_cache(zc)
        info = ServiceInfo(type_, registration_name)
        info.load_from_cache(zc)
        assert info.addresses == []

        out = r.DNSOutgoing(const._FLAGS_QR_QUERY)
        out.add_question(r.DNSQuestion(type_.upper(), const._TYPE_PTR, const._CLASS_IN))
        zc.send(out)
        time.sleep(0.5)
        info = ServiceInfo(type_, registration_name)
        info.load_from_cache(zc)
        assert info.addresses == [socket.inet_pton(socket.AF_INET, "1.2.3.4")]
        assert info.properties == {b"version": b"1.0"}
        zc.close()


class TestServiceRegistry(unittest.TestCase):
    def test_only_register_once(self):
        type_ = "_test-srvc-type._tcp.local."
        name = "xxxyyy"
        registration_name = "%s.%s" % (name, type_)

        desc = {'path': '/~paulsm/'}
        info = ServiceInfo(
            type_, registration_name, 80, 0, 0, desc, "ash-2.local.", addresses=[socket.inet_aton("10.0.1.2")]
        )

        registry = r.ServiceRegistry()
        registry.add(info)
        self.assertRaises(r.ServiceNameAlreadyRegistered, registry.add, info)
        registry.remove(info)
        registry.add(info)

    def test_lookups(self):
        type_ = "_test-srvc-type._tcp.local."
        name = "xxxyyy"
        registration_name = "%s.%s" % (name, type_)

        desc = {'path': '/~paulsm/'}
        info = ServiceInfo(
            type_, registration_name, 80, 0, 0, desc, "ash-2.local.", addresses=[socket.inet_aton("10.0.1.2")]
        )

        registry = r.ServiceRegistry()
        registry.add(info)

        assert registry.get_service_infos() == [info]
        assert registry.get_info_name(registration_name) == info
        assert registry.get_infos_type(type_) == [info]
        assert registry.get_infos_server("ash-2.local.") == [info]
        assert registry.get_types() == [type_]


def test_ptr_optimization():

    # instantiate a zeroconf instance
    zc = Zeroconf(interfaces=['127.0.0.1'])

    # service definition
    type_ = "_test-srvc-type._tcp.local."
    name = "xxxyyy"
    registration_name = "%s.%s" % (name, type_)

    desc = {'path': '/~paulsm/'}
    info = ServiceInfo(
        type_, registration_name, 80, 0, 0, desc, "ash-2.local.", addresses=[socket.inet_aton("10.0.1.2")]
    )

    nbr_answers = nbr_additionals = nbr_authorities = 0
    has_srv = has_txt = has_a = False

    # register
    zc.register_service(info)
    nbr_answers = nbr_additionals = nbr_authorities = 0

    # query
    query = r.DNSOutgoing(const._FLAGS_QR_QUERY | const._FLAGS_AA)
    query.add_question(r.DNSQuestion(info.type, const._TYPE_PTR, const._CLASS_IN))
    out = zc.query_handler.response(r.DNSIncoming(query.packets()[0]), False)
    assert out is not None
    nbr_answers += len(out.answers)
    nbr_authorities += len(out.authorities)
    for answer in out.additionals:
        nbr_additionals += 1
        if answer.type == const._TYPE_SRV:
            has_srv = True
        elif answer.type == const._TYPE_TXT:
            has_txt = True
        elif answer.type == const._TYPE_A:
            has_a = True
    assert nbr_answers == 1 and nbr_additionals == 3 and nbr_authorities == 0
    assert has_srv and has_txt and has_a

    # unregister
    zc.unregister_service(info)
    zc.close()


def test_notify_listeners():
    """Test adding and removing notify listeners."""
    # instantiate a zeroconf instance
    zc = Zeroconf(interfaces=['127.0.0.1'])
    notify_called = 0

    class TestNotifyListener(r.NotifyListener):
        def notify_all(self):
            nonlocal notify_called
            notify_called += 1

    with pytest.raises(NotImplementedError):
        r.NotifyListener().notify_all()

    notify_listener = TestNotifyListener()

    zc.add_notify_listener(notify_listener)

    def on_service_state_change(zeroconf, service_type, state_change, name):
        """Dummy service callback."""

    # start a browser
    browser = ServiceBrowser(zc, "_http._tcp.local.", [on_service_state_change])
    browser.cancel()

    assert notify_called
    zc.remove_notify_listener(notify_listener)

    notify_called = 0
    # start a browser
    browser = ServiceBrowser(zc, "_http._tcp.local.", [on_service_state_change])
    browser.cancel()

    assert not notify_called

    zc.close()
