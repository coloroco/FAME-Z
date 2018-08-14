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
    from famez_switch import switch_handler
    from invariants import ServerInvariant
    from ivshmem_eventfd import ivshmem_event_notifier_list
    from ivshmem_sendrecv import ivshmem_send_one_msg
except ImportError as e:
    from .commander import Commander
    from .famez_mailbox import FAMEZ_MailBox
    from .famez_switch import switch_handler
    from .invariants import ServerInvariant
    from .ivshmem_eventfd import ivshmem_event_notifier_list
    from .ivshmem_sendrecv import ivshmem_send_one_msg

# Don't use peer ID 0, certain docs imply it's reserved.  Put the clients
# from 1 - nClients, and the server goes at nClients + 1.

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
        self.create_new_peer_id()

    @property
    def nodename(self):
        '''For Commander prompt'''
        return self.mailbox.nodename

    def logPrefix(self):    # This override works after instantiation
        return 'ProtoIVSHMSG'

    def dataReceived(self, data):
        ''' TNSH :-) '''
        self.logmsg('dataReceived, quite unexpectedly')
        raise NotImplementedError(self)

    # Use variables that resemble the QEMU "ivshmem-server.c":  peer == self.
    # If errors occur early enough, send a bad revision to the client so it
    # terminates the connection.
    def connectionMade(peer):
        recycled = peer.SI.recycled.get(peer.id, None)
        if recycled:
            del peer.SI.recycled[recycled.id]
        msg = 'new socket %d == peer id %d %s' % (
              peer.transport.fileno(), peer.id,
              'recycled' if recycled else ''
        )
        peer.SI.logmsg(msg)
        if peer.id == -1:           # set from __init__
            peer.SI.logmsg('Max clients reached')
            peer.send_initial_info(False)   # client complains but with grace
            return
        server_peer_list = peer.SI.peer_list

        # Server line 175: create specified number of eventfds.  These are
        # shared with all other clients who use them to signal each other.
        if recycled:
            peer.vectors = recycled.vectors
        else:
            try:
                peer.vectors = ivshmem_event_notifier_list(peer.SI.nEvents)
            except Exception as e:
                peer.SI.logmsg('Event notifiers failed: %s' % str(e))
                peer.send_initial_info(False)
                return

        # Server line 183: send version, peer id, shm fd
        peer.send_initial_info()

        # Server line 189: advertise the new peer to others.  Note that
        # this new peer has not yet been added to the list; this loop is
        # NOT traversed for the first peer to connect.
        if not recycled:
            for other_peer in server_peer_list:
                for peer_vector in peer.vectors:
                    ivshmem_send_one_msg(
                        other_peer.transport.socket,
                        peer.id,
                        peer_vector.wfd)

        # Server line 197: advertise the other peers to the new one.
        for other_peer in server_peer_list:
            for other_peer_vector in other_peer.vectors:
                ivshmem_send_one_msg(
                    peer.transport.socket,
                    other_peer.id,
                    other_peer_vector.wfd)

        # Non-standard voodoo extension to previous advertisment: advertise
        # this server to the new peer.  To QEMU it just looks like one more
        # grouping in the previous batch.  Exists only in non-silent mode.
        for server_vector in peer.server_notifiers:
            ivshmem_send_one_msg(
                peer.transport.socket,
                peer.SI.server_id,
                server_vector.wfd)

        # Server line 205: advertise the new peer to itself, ie, send the
        # eventfds it needs for receiving messages.  This final batch
        # where the embedded peer.id matches the initial_info id is the
        # sentinel that communications are finished.
        for peer_vector in peer.vectors:
            ivshmem_send_one_msg(
                peer.transport.socket,
                peer.id,
                peer_vector.get_fd())   # Must be a good story here...

        # Oh yeah
        server_peer_list.append(peer)

    def connectionLost(self, reason):
        '''Tell the other peers that this one has died.'''
        if reason.check(TIError.ConnectionDone) is None:    # Dirty
            txt = 'Dirty'
        else:
            txt = 'Clean'
        self.SI.logmsg('%s disconnect from peer id %d' % (txt, self.id))
        if self in self.SI.peer_list:     # Only if everything was completed
            self.SI.peer_list.remove(self)
        if self.args.recycle:
            self.SI.recycled[self.id] = self
            return

        try:
            for other_peer in self.SI.peer_list:
                ivshmem_send_one_msg(other_peer.transport.socket, self.id)

            for vector in self.vectors:
                vector.cleanup()

            # For QEMU crashes and shutdowns.  Not the VM, but QEMU itself.
            self.SI.mailbox.clear_my_mailslot(override_id=self.id)

        except Exception as e:
            self.SI.logmsg('Closing peer transports failed: %s' % str(e))

    def create_new_peer_id(peer):
        '''Determine the lowest unused client ID and set peer.id.'''
        # Does not occur often, don't sweat the performance.

        if len(peer.SI.peer_list) >= peer.SI.nClients:
            peer.id = -1    # sentinel
            return  # Until a Link RFC is executed
        peer.SID0 = 0    # Until a CtrlWrite is received
        peer.CID0 = 0
        current_ids = frozenset((p.id for p in peer.SI.peer_list))
        if not current_ids:
            peer.id = 1
            return
        max_ids = frozenset((range(peer.SI.nClients + 2))) - \
                  frozenset((IVSHMEM_UNUSED_ID, peer.SI.server_id ))
        peer.id = sorted(max_ids - current_ids)[0]

    def send_initial_info(peer, ok=True):   # keep the convention self=="peer"
        # Protocol version without fd.
        thesocket = peer.transport.socket
        if not ok:  # Violate the version check and bomb the client.
            ivshmem_send_one_msg(thesocket, -1)
            peer.transport.loseConnection()
            peer.id = -1
            return
        ivshmem_send_one_msg(thesocket, peer.SERVER_IVSHMEM_PROTOCOL_VERSION)

        # The client's id, without an fd.
        ivshmem_send_one_msg(thesocket, peer.id)

        # -1 for data with the fd of the ivshmem file.  Using this protocol
        # a valid fd is required.
        # FIXME does send_one message belong in mailbox.py?
        ivshmem_send_one_msg(thesocket, -1, peer.mailbox.fd)

    # Running in the context of the server, this will retrieve
    # the sender object.
    @staticmethod
    def ServerCallback(vectorobj):
        SI = vectorobj.cbdata
        sender_id = vectorobj.num
        sendername, msg = SI.mailbox.retrieve(sender_id)
        print('%d:%s -> "%s"' % (sender_id, sendername, msg),
            file=sys.stderr)
        # SI.logmsg('"%s" (%d) -> "%s"' % (sendername, sender_id, msg))

        # Find the peer in the list, just to insure this isn't spurious.
        # FIXME: convert to dict{} like client.
        for peer in SI.peer_list:
            if peer.id == sender_id:
                break
        else:
            SI.logmsg('Disappeering act by %d' % sender_id)
            return

        # Try the big shortcut.
        if msg == 'ping':
            SI.logmsg('PONG to %d' % sender_id, file=sys.stderr)
            # Could use "peer.mailbox" as once again it's a class distinction
            # but this reads better.
            peer.mailbox.fill(SI.server_id, 'PONG')
            peer.vectors[SI.server_id].incr()
            return

        switch_handler(peer, msg)

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
        args.mailbox = FAMEZ_MailBox(args=args)  # replace string with object

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

