# This effort [has been superseded by linux-genz](http://github.com/linux-genz)

[Gen-Z is a new memory-semantic fabrique](https://genzconsortium.org/) created
as the glue for constructing exascale computing.  It is an open specification
evolved from the fabrique used in
[The Machine from Hewlett Packard Enterprise](https://www.hpe.com/TheMachine).
Such fabriques allow "wide-area" connectivity of computing resources such as CPU,
GPU, memory (legacy and persistent) and other devices via a memory-semantic
programming model.

The Gen-Z spec and working groups are evolving the standard and early
hardware is beginning to appear.  However there is not an open "platform"
on which to develop system software.  The success of QEMU and IVSHMEM as
[an emulated development platform for The Machine](docs/FAME_background.md)
suggests an extended use should be considered. 
  
### Beyond IVSHMEM - a rudimentary fabrique

QEMU has another feature of interest in a multi-actor messaging environment
like that of Gen-Z.  By applying a slightly different stanza, the IVSHMEM
virtual PCI device is enabled to send and handle interrupts in a
"mailbox/doorbell" setup.   An interrupt to the virtual PCI device is generated
from an "event notification" issued to the QEMU process by a similarly
configured peer QEMU.  But how are these peers connected?

The scheme starts with a separate program delivered with QEMU. [
```/usr/bin/ivshmem-server ```](
https://github.com/qemu/qemu/blob/master/docs/specs/ivshmem-spec.txt)
establishes a UNIX-domain socket and must be started before any properly
configured QEMU VMs.  A new QEMU process starts by connecting to the socket
and receiving its own set of event channels, as well as those of all other
peers.  The mechanism in each guest OS is that writing a "doorbell" register
will signal the QEMU into an event against another QEMU.  The receiving QEMU
transforms that event into a PCI interrupt for its guest OS.  

```ivshmem-server``` only informs each QEMU of its other peers; it does not
participate in further peer-to-peer communcation.  A backing file must also
be specified to ivshmem-server for use as a message mailbox.  Obviously the
guests/clients must agree on the use of the mailbox file.  Standard
ivshmem-server never touches the file contents.

![alt text][IVSHMSG]

[IVSHMSG]: https://github.com/coloroco/FAME-Z/blob/master/docs/images/IVSHMSG%20block.png "Figure 1"

The final use case above is QEMU guest-to-guest communication over the "IVSHMSG
doorbell/mailbox fabrique".  OS-to-OS communication will involve a (new) guest
kernel driver and other abstractions to hide the mechanics of IVSHMSG.
This IVSHMSG shim can serve as the foundation for higher-level protocols.

## Gen-Z Emulation on top of IVSHMSG

If the guest OS driver emulates a simple Gen-Z bridge, a great deal of
"pure Gen-Z" software development can be done on this simple platform.
Certain Gen-Z primitive operations for discovery and crawlout
would also be abetted by intelligence "in the fabrique".  In fact, that 
intelligence could live in the ivshmem-server process, and it could be 
extended to participate in actual.

Modifying the existing ```ivshmem-server`` C program is not a simple challenge.
Written within the QEMU build framework, it is not standalone source code.
C is a also limited for higher-level data constructs anticipated for a Gen-Z
emulation.  Finally, it seems unlikely such changes would be accepted upstream.

This project is a rewrite of ivshmem-server in Python using Twisted
as the network-handling framework.  ```ivshmsg_server.py``` is run in place of
```ivshmem-server```.  It correctly serves real QEMU processes as well as
the stock QEMU ``ivshmem-client``, a test program that comes with QEMU.

![alt text][EMERGEN-Z]

[EMERGEN-Z]: https://github.com/coloroco/FAME-Z/blob/master/docs/images/FAME-Z%20block.png "Figure 2"

A new feature for the Python version is participation 
in the doorbell/mailbox messaging to serve as fabrique intelligence
(ie, a smart switch).

## Running the Python rewrites

As ivshmsg_server.py was being created, it was tested with the QEMU
```/usr/bin/ivshmem-client```.  As might be expected, there is now an
ivshmsg_client.py rewrite.   It has an expanded command set and over
time its use as a monitor/debugger/injector will certainly grow.

To use these programs as a simple chat framework you don't even need QEMU.

1. Clone this repo
1. Install python3 packages ```twisted``` and ```klein``` (names will vary by distro)
1. In one terminal window run './ivshmsg_server.py'.  This accepts fourteen clients.  By default it creates /tmp/ivshmsg_socket to which clients attach, and /dev/shm/ivshmsg_mailbox which is shared among all clients for messaging.
1. In a second (or more) terminal window(s) run 'ivshmsg_client.py'.  You'll see them get added in the server log output.
1. In one of the clients, hit return, then type "help".  Play with sending messages to the other client(s) or the server.

## Connecting VMs

That's another story [which is going to be told here.](docs/VMconfig.md)

You are advised to get the Python programs playing together before taking
these steps.

## BUGS

As the QEMU docs say, "(IVSHMSG) is simple and fragile" and sometimes
* A QEMU session will hang
* A QEMU session will die
* All QEMUs need a restart
* (Rarely) you have to restart ivshmsg_server.py
* The way I crafted an interlock protocol in the guest kernel drivers can cause a VM to go into RCU stall which usually leads to a virtual panic.

In spite of all that, it's been stable enough to generate a prototype Gen-Z
subsystem for the kernel.  Read all about that in .....wherever it ends up.....
