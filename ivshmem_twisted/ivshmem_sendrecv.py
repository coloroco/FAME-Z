#!/usr/bin/python3

# This work is licensed under the terms of the GNU GPL, version 2 or
# (at your option) any later version.  See the LICENSE file in the
# top-level directory.

# Rocky Craig <rocky.craig@hpe.com>

# Routine names here mirror those in qemu/contrib/ivshmem-[client|server].
# The IVSHMEM communications protocol is based on 8-byte integers and an
# optional 4-byte file descriptor.  Twisted transport.sendFileDescriptor
# gets the packing sizes wrong so do it right.

import socket
import struct

###########################################################################


def ivshmem_send_one_msg(thesocket, data, fd=None):
    # On the far side, if no fd is received from here, a helper routine
    # returns fd == -1 which is checked in various places.
    data = int(data)
    bdata_iovec = [ struct.pack('q', data) ]    # One item in the vector
    if fd is None:
        cmsg = []   # Message array of none, defaults to fd == -1 on far side.
    else:
        fd = int(fd)
        cmsg = [    # Message array of one.
            (socket.SOL_SOCKET, socket.SCM_RIGHTS, struct.pack('i', fd))
        ]
    ret = thesocket.sendmsg(bdata_iovec, cmsg)
    assert ret == 8, 'sendmsg could not write 8 bytes'
