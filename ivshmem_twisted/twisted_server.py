#!/usr/bin/python3

# This work is licensed under the terms of the GNU GPL, version 2 or
# (at your option) any later version.  See the LICENSE file in the
# top-level directory.

# Rocky Craig <rocky.craig@hpe.com>

import argparse
import grp
import mmap
import os
import struct
import sys

from collections import OrderedDict
from pprint import pprint

from twisted.python import log as TPlog
from twisted.python.logfile import DailyLogFile

from twisted.internet import error as TIError
from twisted.internet import reactor as TIreactor

from twisted.internet.endpoints import UNIXServerEndpoint

from twisted.internet.protocol import ServerFactory as TIPServerFactory
from twisted.internet.protocol import Protocol as TIPProtocol

try:
    from commander import Commander
    from famez_mailbox import FAMEZ_MailBox
    from famez_requests import request_handler
    from general import ServerInvariant
    from ivshmem_eventfd import ivshmem_event_notifier_list, EventfdReader
    from ivshmem_sendrecv import ivshmem_send_one_msg
except ImportError as e:
    from .commander import Commander
    from .famez_mailbox import FAMEZ_MailBox
    from .famez_requests import request_handler
    from .general import ServerInvariant
    from .ivshmem_eventfd import ivshmem_event_notifier_list, EventfdReader
    from .ivshmem_sendrecv import ivshmem_send_one_msg

# Don't use peer ID 0, certain docs imply it's reserved.  Put the clients
# from 1 - nClients, and the server goes at nClients + 1.  Then use slot
# 0 as global data storage, primarily the server command-line arguments.

IVSHMEM_UNUSED_ID = 0

###########################################################################
# See qemu/docs/specs/ivshmem-spec.txt::Client-Server protocol and
# qemu/contrib/ivshmem-server.c::ivshmem_server_handle_new_conn() calling
# qemu/contrib/ivshmem-server.c::ivshmem_server_send_initial_info(), then
# qemu/contrib/ivshmem-client.c::ivshmem_client_connect()


class ProtocolIVSHMSGServer(TIPProtocol):

    SERVER_IVSHMEM_PROTOCOL_VERSION = 0

    SI = None

    def __init__(self, factory):
        '''"self" is a new client connection, not "me" the server.'''
        if self.SI is None:
            assert isinstance(factory, TIPServerFactory), 'arg0 not my Factory'
            self.__class__.SI = ServerInvariant(factory.cmdlineargs)
            SI = self.SI
            SI.C_Class = 'Switch'

            # Non-standard addition to IVSHMEM server role: this server can be
            # interrupted and messaged to particpate in client activity.
            # It will get looped even if it's empty (silent mode).
            SI.EN_list = []

            # Usually create eventfds for receiving messages in IVSHMSG and
            # set up a callback.  This early arming is not a race condition
            # as the peer for which this is destined has not yet been told
            # of the fds it would use to trigger here.

            if not factory.cmdlineargs.silent:
                SI.EN_list = ivshmem_event_notifier_list(SI.nEvents)
                # The actual client doing the sending needs to be fished out
                # via its "num" vector.
                for i, EN in enumerate(SI.EN_list):
                    EN.num = i
                    tmp = EventfdReader(EN, self.ServerCallback, SI)
                    if i:   # Technically it blocks mailslot 0, the globals
                        tmp.start()

        self.create_new_peer_id()

    @property
    def nodename(self):
        '''For Commander prompt'''
        return 'Z-switch' if self.SI.args.smart else 'Z-server'

    def logPrefix(self):    # This override works after instantiation
        return 'ProtoIVSHMSG'

    def dataReceived(self, data):
        ''' TNSH :-) '''
        self.SI.logmsg('dataReceived, quite unexpectedly')
        raise NotImplementedError(self)

    # If errors occur early enough, send a bad revision to the client so it
    # terminates the connection.  Remember, "self" is a proxy for a peer.
    def connectionMade(self):
        recycled = self.SI.recycled.get(self.id, None)
        if recycled:
            del self.SI.recycled[recycled.id]
        msg = 'new socket %d == peer id %d %s' % (
              self.transport.fileno(), self.id,
              'recycled' if recycled else ''
        )
        self.SI.logmsg(msg)
        if self.id == -1:           # set from __init__
            self.SI.logmsg('Max clients reached')
            self.send_initial_info(False)   # client complains but with grace
            return
        server_peer_list = self.SI.peer_list

        # Server line 175: create specified number of eventfds.  These are
        # shared with all other clients who use them to signal each other.
        if recycled:
            self.EN_list = recycled.EN_list
        else:
            try:
                self.EN_list = ivshmem_event_notifier_list(self.SI.nEvents)
            except Exception as e:
                self.SI.logmsg('Event notifiers failed: %s' % str(e))
                self.send_initial_info(False)
                return

        # Server line 183: send version, peer id, shm fd
        if not self.send_initial_info():
            self.SI.logmsg('Send initial info failed')
            return

        # Server line 189: advertise the new peer to others.  Note that
        # this new peer has not yet been added to the list; this loop is
        # NOT traversed for the first peer to connect.
        if not recycled:
            for other_peer in server_peer_list:
                for peer_EN in self.EN_list:
                    ivshmem_send_one_msg(
                        other_peer.transport.socket,
                        self.id,
                        peer_EN.wfd)

        # Server line 197: advertise the other peers to the new one.
        # Remember "this" new peer proxy has not been added to the list yet.
        for other_peer in server_peer_list:
            for other_peer_EN in other_peer.EN_list:
                ivshmem_send_one_msg(
                    self.transport.socket,
                    other_peer.id,
                    other_peer_EN.wfd)

        # Non-standard voodoo extension to previous advertisment: advertise
        # this server to the new peer.  To QEMU it just looks like one more
        # grouping in the previous batch.  Exists only in non-silent mode.
        for server_EN in self.SI.EN_list:
            ivshmem_send_one_msg(
                self.transport.socket,
                self.SI.server_id,
                server_EN.wfd)

        # Server line 205: advertise the new peer to itself, ie, send the
        # eventfds it needs for receiving messages.  This final batch
        # where the embedded self.id matches the initial_info id is the
        # sentinel that communications are finished.
        for peer_EN in self.EN_list:
            ivshmem_send_one_msg(
                self.transport.socket,
                self.id,
                peer_EN.get_fd())   # Must be a good story here...

        # Oh yeah
        server_peer_list.append(self)

        msg = 'Link CTL Peer-Attribute'
        FAMEZ_MailBox.fill(self.SI.server_id, msg)
        self.EN_list[self.SI.server_id].incr()

    def connectionLost(self, reason):
        '''Tell the other peers that this one has died.'''
        if reason.check(TIError.ConnectionDone) is None:    # Dirty
            txt = 'Dirty'
        else:
            txt = 'Clean'
        self.SI.logmsg('%s disconnect from peer id %d' % (txt, self.id))
        if self in self.SI.peer_list:     # Only if everything was completed
            self.SI.peer_list.remove(self)
        if self.SI.args.recycle:
            self.SI.recycled[self.id] = self
            return

        try:
            for other_peer in self.SI.peer_list:
                ivshmem_send_one_msg(other_peer.transport.socket, self.id)

            for EN in self.EN_list:
                EN.cleanup()

            # For QEMU crashes and shutdowns.  Not the VM, but QEMU itself.
            FAMEZ_MailBox.clear_mailslot(self.id)

        except Exception as e:
            self.SI.logmsg('Closing peer transports failed: %s' % str(e))

    def create_new_peer_id(self):
        '''Determine the lowest unused client ID and set self.id.'''

        self.SID0 = 0
        self.CID0 = 0
        if len(self.SI.peer_list) >= self.SI.nClients:
            self.id = -1    # sentinel
            return  # Until a Link RFC is executed
        current_ids = frozenset((p.id for p in self.SI.peer_list))
        if not current_ids:
            self.id = 1
        else:
            max_ids = frozenset((range(self.SI.nClients + 2))) - \
                      frozenset((IVSHMEM_UNUSED_ID, self.SI.server_id ))
            self.id = sorted(max_ids - current_ids)[0]
        if self.SI.args.smart:
            self.SID0 = self.SI.default_SID
            self.CID0 = self

    def send_initial_info(self, ok=True):
        thesocket = self.transport.socket   # self is a proxy for the peer.
        try:
            # 1. Protocol version without fd.
            if not ok:  # Violate the version check and bomb the client.
                print('Early termination', file=sys.stderr)
                ivshmem_send_one_msg(thesocket, -1)
                self.transport.loseConnection()
                self.id = -1
                return
            if not ivshmem_send_one_msg(thesocket,
                self.SERVER_IVSHMEM_PROTOCOL_VERSION):
                print('This is screwed', file=sys.stderr)
                return False

            # 2. The client's (new) id, without an fd.
            ivshmem_send_one_msg(thesocket, self.id)

            # 3. -1 for data with the fd of the ivshmem file.  Using this
            # protocol a valid fd is required.
            ivshmem_send_one_msg(thesocket, -1, FAMEZ_MailBox.fd)
            return True
        except Exception as e:
            print(str(e), file=sys.stderr)
        return False

    # Match the signature of twisted_client object so they're both compliant
    # with downstream processing.  General lookup form is [dest][src], ie,
    # first get the list for dest, then pick out src ("from") trigger EN.
    def responder_EN(responder, requester_id, responder_id):
        return responder.EN_list[responder_id]  # requester is not used

    # The cbdata is a class variable common to all requester proxy objects.
    # The object which serves as the responder needs to be calculated.
    @staticmethod
    def ServerCallback(vectorobj):
        requester_id = vectorobj.num
        requester_name, request = FAMEZ_MailBox.retrieve(requester_id)
        SI = vectorobj.cbdata
        responder_id = SI.server_id

        trace = '%10s@%d->"%s" (%d)' % (
            requester_name, requester_id, request, len(request))
        print(trace, file=sys.stderr)

        # The requester can die between its request and this callback.
        # peer_list[] is the proxy objects, one for each original connection.
        for responder in SI.peer_list:
            if responder.id == requester_id:
                break
        else:
            SI.logmsg('Disappeering act by %d' % requester_id)
            return
        responder_EN = responder.responder_EN(requester_id, responder_id)

        request_handler(request, responder, responder_id, responder_EN)

    #----------------------------------------------------------------------
    # Command line parsing.

    def doCommand(self, cmd, args):

        if cmd in ('h', 'help') or '?' in cmd:
            print('h[elp]\n\tThis message')
            print('s[tatus]\n\tStatus of all ports')
            print('q[uit]\n\tShut it all down')
            return True

        if cmd in ('s', 'status'):
            print('Oooh shiny')
            for peer in self.SI.peer_list:
                pprint(args(peer), stream=sys.stdout)
            return True

        if cmd in ('q', 'quit'):
            self.transport.loseConnection()
            return False

        raise NotImplementedError('asdf')

###########################################################################
# Normally the Endpoint and listen() call is done explicitly, interwoven
# with passing this constructor.  This approach used here hides all the
# twisted things in this module.


class FactoryIVSHMSGServer(TIPServerFactory):

    _required_arg_defaults = {
        'foreground':   True,       # Only affects logging choice in here
        'logfile':      '/tmp/ivshmem_log',
        'mailbox':      'ivshmem_mailbox',  # Will end up in /dev/shm
        'nClients':     2,
        'recycle':      False,      # Try to preserve other QEMUs
        'silent':       False,      # Does participate in eventfds/mailbox
        'socketpath':   '/tmp/ivshmem_socket',
        'verbose':      0,
    }

    def __init__(self, args=None):
        '''Args must be an object with the following attributes:
           foreground, logfile, mailbox, nClients, silent, socketpath, verbose
           Suitable defaults will be supplied.'''

        # Pass command line args to ProtocolIVSHMSG, then open logging.
        if args is None:
            args = argparse.Namespace()
        for arg, default in self._required_arg_defaults.items():
            setattr(args, arg, getattr(args, arg, default))

        # Mailbox may be sized above the requested number of clients to
        # satisfy QEMU IVSHMEM restrictions.
        args.server_id = args.nClients + 1
        args.nEvents = args.nClients + 2
        FAMEZ_MailBox(args=args)  # singleton class, no need to keep instance

        self.cmdlineargs = args
        if args.foreground:
            TPlog.startLogging(sys.stdout, setStdout=False)
        else:
            print('Logging to %s' % args.logfile, file=sys.stderr)
            TPlog.startLogging(
                DailyLogFile.fromFullPath(args.logfile),
                setStdout=True)     # "Pass-through" explicit print() for debug
        args.logmsg = TPlog.msg
        args.logerr = TPlog.err

        # By Twisted version 18, "mode=" is deprecated and you should just
        # inherit the tacky bit from the parent directory.  wantPID creates
        # <path>.lock as a symlink to "PID".
        E = UNIXServerEndpoint(
            TIreactor,
            args.socketpath,
            mode=0o666,         # Deprecated at Twisted 18
            wantPID=True)
        E.listen(self)
        args.logmsg('FAME-Z server (id=%d) listening for up to %d clients on %s' %
            (args.server_id, args.nClients, args.socketpath))

        # https://stackoverflow.com/questions/1411281/twisted-listen-to-multiple-ports-for-multiple-processes-with-one-reactor

    def buildProtocol(self, useless_addr):
        # Docs mislead, have to explicitly pass something to get persistent
        # state across protocol/transport invocations.  As there is only
        # one server object per process instantion, that's not necessary.
        protobj = ProtocolIVSHMSGServer(self)
        Commander(protobj)
        return protobj

    def run(self):
        TIreactor.run()

