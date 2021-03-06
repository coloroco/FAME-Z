/*
 * Copyright (C) 2018 Hewlett Packard Enterprise Development LP.
 * All rights reserved.
 *
 * This software is available to you under a choice of one of two
 * licenses.  You may choose to be licensed under the terms of the GNU
 * General Public License (GPL) Version 2, available from the file
 * COPYING in the main directory of this source tree, or the
 * BSD license below:
 *
 * Redistribution and use in source and binary forms, with or without
 * modification, are permitted provided that the following conditions
 * are met:
 *
 *   * Redistributions of source code must retain the above copyright
 *     notice, this list of conditions and the following disclaimer.
 *
 *   * Redistributions in binary form must reproduce the above
 *     copyright notice, this list of conditions and the following
 *     disclaimer in the documentation and/or other materials provided
 *     with the distribution.
 *
 * THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
 * "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
 * LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
 * FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
 * COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
 * INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
 * BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
 * LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
 * CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
 * LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
 * ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
 * POSSIBILITY OF SUCH DAMAGE.
 */

#include <linux/ctype.h>
#include <linux/fs.h>
#include <linux/kernel.h>
#include <linux/list.h>
#include <linux/miscdevice.h>
#include <linux/pci.h>
#include <linux/poll.h>
#include <linux/spinlock.h>
#include <linux/wait.h>

#include <asm-generic/bug.h>	// yes after the others

#include "famez.h"
#include "famez_bridge.h"

static int fzbridge_verbose = 2;

DECLARE_WAIT_QUEUE_HEAD(bridge_reader_wait);

// https://stackoverflow.com/questions/39464028/device-specific-data-structure-with-platform-driver-and-character-device-interfa
// A lookup table to take advantage of misc_register putting its argument
// into file->private at open().  Fill in the blanks for each config and go.
// This technique relies on the desired field being a pointer AND the first
// field, so that "container_of(..., anchor)" is a pointer to a pointer.
// I modified the article's solution to treat it as a container pointer and
// just grab whatever field I want, it doesn't even have to be the first one.
// If I put the "primary key" structure as the first field, then I wouldn't
// even need container_of as the address is synonymous with both.

typedef struct {
	struct miscdevice miscdev;	// full structure, not a ptr
	famez_configuration_t *config;	// what I want to recover
} miscdev2config_t;

static inline famez_configuration_t *extract_config(struct file *file)
{
	struct miscdevice *encapsulated_miscdev = file->private_data;
	miscdev2config_t *lookup = container_of(
		encapsulated_miscdev,	// the pointer to the member
		miscdev2config_t,	// the type of the container struct
		miscdev);		// the name of the member in the struct
	return lookup->config;
}

//-------------------------------------------------------------------------
// file->private is set to the miscdevice structure used in misc_register.

static int bridge_open(struct inode *inode, struct file *file)
{
	famez_configuration_t *config = extract_config(file);
	int n, ret;

	// FIXME: got to come up with more 'local module' support for this.
	// Just keep it single user for now.
	ret = 0;
	if ((n = atomic_add_return(1, &config->nr_users) == 1)) {
		bridge_buffers_t *buffers;

		if (!(buffers = kzalloc(sizeof(bridge_buffers_t), GFP_KERNEL))) {
			ret = -ENOMEM;
			goto alldone;
		}
		if (!(buffers->wbuf = kzalloc(config->max_msglen, GFP_KERNEL))) {
			kfree(buffers);
			ret = -ENOMEM;
			goto alldone;
		}
		FAMEZ_LOCK_INIT(&(buffers->wbuf_lock));
		config->writer_support = buffers;
	} else {
		pr_warn(FZBRSP "Sorry, just exclusive open() for now\n");
		ret = -EBUSY;
		goto alldone;
	}

	PR_V1("open: %d users\n", atomic_read(&config->nr_users));

alldone:
	if (ret) 
		atomic_dec(&config->nr_users);
	return ret;
}

//-------------------------------------------------------------------------
// At any close of a process fd

static int bridge_flush(struct file *file, fl_owner_t id)
{
	famez_configuration_t *config = extract_config(file);
	int nr_users, f_count;

	spin_lock(&file->f_lock);
	nr_users = atomic_read(&config->nr_users);
	f_count = atomic_long_read(&file->f_count);
	spin_unlock(&file->f_lock);
	if (f_count == 1) {
		atomic_dec(&config->nr_users);
		nr_users--;
	}

	PR_V1("flush: after (optional) dec: %d users, file count = %d\n",
		nr_users, f_count);
	
	return 0;
}

//-------------------------------------------------------------------------
// Only at the final close of the last process fd

static int bridge_release(struct inode *inode, struct file *file)
{
	famez_configuration_t *config = extract_config(file);
	bridge_buffers_t *buffers = config->writer_support;
	int nr_users, f_count;

	spin_lock(&file->f_lock);
	nr_users = atomic_read(&config->nr_users);
	f_count = atomic_long_read(&file->f_count);
	spin_unlock(&file->f_lock);
	PR_V1("release: %d users, file count = %d\n", nr_users, f_count);
	BUG_ON(nr_users);
	kfree(buffers->wbuf);
	kfree(buffers);
	config->writer_support = NULL;
	return 0;
}

//-------------------------------------------------------------------------
// Final step on the way to stacked modules, also code reuse for
// bridge_read and bridge_ioctl.  Failure returns < 0 without lock held;
// success returns >=0 number of bytes ready with lock held.
// Intermix locking with that in msix_all().

famez_mailslot_t *famez_await_legible_slot(struct file *file,
					   famez_configuration_t *config)
{
	int ret;

	if ((ret = FAMEZ_LOCK(&config->legible_slot_lock)))
		return ERR_PTR(ret);
	while (!config->legible_slot) {		// Wait for new data?
		FAMEZ_UNLOCK(&config->legible_slot_lock);
		if (file->f_flags & O_NONBLOCK)
			return ERR_PTR(-EAGAIN);
		PR_V2(FZ "read() waiting...\n");
		if (wait_event_interruptible(config->legible_slot_wqh, 
					     config->legible_slot))
			return ERR_PTR(-ERESTARTSYS);
		if ((ret = FAMEZ_LOCK(&config->legible_slot_lock)))
			return ERR_PTR(ret);
	}
	return config->legible_slot;
}

void famez_release_legible_slot(famez_configuration_t *config)
{
	config->legible_slot->msglen = 0;	// In the slot of the remote sender
	config->legible_slot = NULL;		// Seen by local MSIX handler
	FAMEZ_UNLOCK(&config->legible_slot_lock);
}

//-------------------------------------------------------------------------
// Prepend the sender id as a field separated by a colon, realized by two
// calls to copy_to_user and avoiding a temporary buffer here. copy_to_user
// can sleep and returns the number of bytes that could NOT be copied or
// -ERRNO.  Require both copies to work all the way.  

#define SENDER_ID_FMT	"%03llu:"	// These must agree
#define SENDER_ID_LEN	4

static ssize_t bridge_read(struct file *file, char __user *buf,
			   size_t buflen, loff_t *ppos)
{
	famez_configuration_t *config = extract_config(file);
	famez_mailslot_t *legible_slot;
	char sender_id_str[8];
	int ret;

	legible_slot = famez_await_legible_slot(file, config);
	if (IS_ERR(legible_slot))
		return PTR_ERR(legible_slot);
	PR_V2(FZSP "wait finished, %llu bytes to read\n", legible_slot->msglen);
	if (buflen < legible_slot->msglen + SENDER_ID_LEN) {
		ret = -E2BIG;
		goto read_complete;
	}

	// First emit the sender ID.
	sprintf(sender_id_str, SENDER_ID_FMT, config->legible_slot->peer_id);
	if ((ret = copy_to_user(buf, sender_id_str, SENDER_ID_LEN))) {
		if (ret > 0) ret= -EFAULT;	// partial transfer
		goto read_complete;
	}

	// Now the message body proper, after the colon of the previous message.
	ret = copy_to_user(buf + SENDER_ID_LEN,
		legible_slot->msg, legible_slot->msglen);
	ret = ret ? -EFAULT : legible_slot->msglen + SENDER_ID_LEN;

read_complete:	// Whether I used it or not, let everything go
	famez_release_legible_slot(config);
	return ret;
}

//-------------------------------------------------------------------------

static ssize_t bridge_write(struct file *file, const char __user *buf,
			    size_t buflen, loff_t *ppos)
{
	famez_configuration_t *config = extract_config(file);
	bridge_buffers_t *buffers = config->writer_support;
	ssize_t successlen = buflen;
	char *msgbody;
	int ret;
	uint16_t peer_id;

	// Use many idiot checks.  Performance is not the issue here.
	// FIXME It could be raw data with nulls at some point...
	if (buflen >= config->max_msglen - 1) {	// Paranoia on term NUL
		PR_V1("msglen of %lu is too big\n", buflen);
		return -E2BIG;
	}
	if ((ret = FAMEZ_LOCK(&buffers->wbuf_lock)))	// Multiuse of *file
		goto unlock_return;
	if (copy_from_user(buffers->wbuf, buf, buflen)) {
		ret = -EIO;
		goto unlock_return;
	}
	buffers->wbuf[buflen] = '\0';

	// Split body into two strings around the first colon.
	if (!(msgbody = strchr(buffers->wbuf, ':'))) {
		pr_err(FZBR "I see no colon in \"%s\"\n", buffers->wbuf);
		ret = -EBADMSG;
		goto unlock_return;
	}
	*msgbody = '\0';	// chomp ':', now two complete strings
	msgbody++;
	buflen -= (uint64_t)msgbody - (uint64_t)buffers->wbuf;

	if (STREQ(buffers->wbuf, "server"))
		peer_id = config->server_id;
	else {
		if ((ret = kstrtou16(buffers->wbuf, 10, &peer_id)))
			goto unlock_return;	// -ERANGE, usually
	}

	// Length or -ERRNO.  If length matched, then all is well, but
	// this final len is always shorter than the original length.  Some
	// code (ie, "echo") will resubmit the partial if the count is
	// short.  So lie about it to the caller.
	ret = famez_sendmail(peer_id, msgbody, buflen, config);
	if (ret == buflen)
		ret = successlen;
	else if (ret >= 0)
		ret = -EIO;	// partial transfer paranoia

unlock_return:
	FAMEZ_UNLOCK(&buffers->wbuf_lock);
	return ret;
}

//-------------------------------------------------------------------------
// Returning 0 will cause the caller (epoll/poll/select) to sleep.

static uint bridge_poll(struct file *file, struct poll_table_struct *wait)
{
	famez_configuration_t *config = extract_config(file);
	uint ret = 0;

	poll_wait(file, &bridge_reader_wait, wait);
		ret |= POLLIN | POLLRDNORM;
	// FIXME encapsulate this better, it's really the purview of sendstring
	if (!config->my_slot->msglen)
		ret |= POLLOUT | POLLWRNORM;
	return ret;
}

static const struct file_operations bridge_fops = {
	.owner	=	THIS_MODULE,
	.open	=	bridge_open,
	.flush  =	bridge_flush,
	.release =      bridge_release,
	.read	=       bridge_read,
	.write	=       bridge_write,
	.poll	=       bridge_poll,
};

//-------------------------------------------------------------------------
// Follow convention of PCI core: all (early) setup takes a pdev.
// The argument of misc_register ends up in file->private_data.

int famez_bridge_setup(struct pci_dev *pdev)
{
	famez_configuration_t *config = pci_get_drvdata(pdev);
	miscdev2config_t *lookup;
	char *name;

	if (!(lookup = kzalloc(sizeof(miscdev2config_t), GFP_KERNEL)))
		return -ENOMEM;
	if (!(name = kzalloc(32, GFP_KERNEL))) {
		kfree(lookup);
		return -ENOMEM;
	}

	// Name is meant to be reminiscent of lspci output
	sprintf(name, "%s%02x_bridge", FAMEZ_NAME, config->pdev->devfn >> 3);
	lookup->miscdev.name = name;
	lookup->miscdev.fops = &bridge_fops;
	lookup->miscdev.minor = MISC_DYNAMIC_MINOR;
	lookup->miscdev.mode = 0666;

	lookup->config = config;	// Don't point that thing at me
	config->teardown_lookup = lookup;
	return misc_register(&lookup->miscdev);
}

//-------------------------------------------------------------------------
// Follow convention of PCI core: all (early) setup takes a pdev.

void famez_bridge_teardown(struct pci_dev *pdev)
{
	famez_configuration_t *config = pci_get_drvdata(pdev);
	miscdev2config_t *lookup = config->teardown_lookup;
	
	misc_deregister(&lookup->miscdev);
	kfree(lookup->miscdev.name);
	kfree(lookup);
	config->teardown_lookup = NULL;
}
