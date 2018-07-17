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

DECLARE_WAIT_QUEUE_HEAD(bridge_reader_wait);

typedef struct {
} bridge_data_t;

//-------------------------------------------------------------------------
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

	// When this is broken, the values tend to look like pointers.
	// Do a poor man's abs().
	if ((uint64_t)atomic_read(&config->nr_users) > 64) {
		pr_err(FZ "Looks like extract_config() is borked\n");
		return -ECANCELED;
	}
	atomic_inc(&config->nr_users);
	PR_V1("open: after inc, %d users\n", atomic_read(&config->nr_users));

	return 0;
}

//-------------------------------------------------------------------------
// At any close of a process fd

static int bridge_flush(struct file *file, fl_owner_t id)
{
	famez_configuration_t *config = extract_config(file);

	atomic_dec_and_test(&config->nr_users);
	PR_V1("flush: after dec, %d users\n", atomic_read(&config->nr_users));
	return 0;
}


//-------------------------------------------------------------------------
// Only at the final close of the last process fd

static int bridge_release(struct inode *inode, struct file *file)
{
	famez_configuration_t *config = extract_config(file);

	PR_V1("release: %d users\n", atomic_read(&config->nr_users));
	atomic_set(&config->nr_users, 0);
	return 0;
}

//-------------------------------------------------------------------------

static ssize_t bridge_read(struct file *file, char __user *buf, size_t len,
                          loff_t *ppos)
{
	famez_configuration_t *config = extract_config(file);
	int ret;
	char sender_id[8];	// sprintf(sender_id, "%03:", ....
	size_t sender_id_len;

	// FIXME: encapsulate this in famez_MSI-X, down to EOENCAPSULATION.
	// Do the same for the resource release at label "release:"
	if (!config->legible_slot) {		// Wait for new data?
		if (file->f_flags & O_NONBLOCK)
			return -EAGAIN;
		PR_V2(FZ "read() waiting...\n");
		wait_event_interruptible(config->legible_slot_wqh, 
					 config->legible_slot);
	}
	if (!config->legible_slot) {
		pr_err(FZ "legible slot is not ready\n");
		return -EBADE;
	}
	PR_V2(FZSP "wait finished, %llu bytes to read\n",
		 config->legible_slot->msglen);
	// EOENCAPSULATION

	sprintf(sender_id, "%03d:", config->legible_slot->peer_id);
	sender_id_len = strlen(sender_id);
	if (len < config->legible_slot->msglen + sender_id_len) {
		ret = -E2BIG;
		goto release;
	}

	// copy_to_user can sleep and returns the number of bytes that could NOT
	// be copied or -ERRNO.  Require all copies to work all the way.  First
	// emit the sender ID so the user knows from whence the message came.
	ret = copy_to_user(buf, sender_id, sender_id_len);
	if (ret) {
		if (ret > 0) ret= -EIO;		// partial transfer
		goto release;
	}

	// Now the message body proper, after the colon of the previous message.
	len = config->legible_slot->msglen;
	ret = copy_to_user(buf + sender_id_len, config->legible_slot->msg, len);
	ret = ret ? -EIO : len + sender_id_len;

release:	// Whether I used it or not, let everything go
	spin_lock(&config->legible_slot_lock);
	config->legible_slot->msglen = 0;	// In the slot of the remote sender
	config->legible_slot = NULL;		// Seen by local MSIX handler
	spin_unlock(&config->legible_slot_lock);
	return ret;
}

//-------------------------------------------------------------------------

static ssize_t bridge_write(struct file *file, const char __user *buf,
			   size_t len, loff_t *ppos)
{
	famez_configuration_t *config = extract_config(file);
	ssize_t spooflen = len;
	char *msgbody;
	int ret;
	uint16_t peer_id;

	// Use many idiot checks.  Performance is not the issue here.
	// It could be raw data with nulls at some point...
	if (len >= config->max_msglen - 1)	// Paranoia on term NUL
		return -E2BIG;

	spin_lock_bh(&config->scratch_lock);	// Multiuse of *file
	if (copy_from_user(config->scratch, buf, len)) {
		ret = -EIO;
		goto unlock_return;
	}
	config->scratch[len] = '\0';

	// Split body into two strings around the first colon.
	if (!(msgbody = strchr(config->scratch, ':'))) {
		pr_err(FZ "I see no colon in \"%s\"\n", config->scratch);
		ret = -EBADMSG;
		goto unlock_return;
	}
	*msgbody = '\0';	// chomp ':', now two complete strings
	msgbody++;
	len -= (uint64_t)msgbody - (uint64_t)config->scratch;

	// FIXME: assumes ASCII data, not binary which may have embedded NULs
	if (!(len = strlen(msgbody))) {
		pr_warn(FZ "msgbody length mismatch\n");
		ret = spooflen;	// spoof success to caller
		goto unlock_return;
	}
	if (STREQ(config->scratch, "server"))
		peer_id = config->server_id;
	else {
		if ((ret = kstrtou16(config->scratch, 10, &peer_id)))
			goto unlock_return;	// -ERANGE, usually
	}

	// Length or -ERRNO.  If length matched, then all is well, but
	// this final len is always shorter than the original length.  Some
	// code (ie, "echo") will resubmit the partial if the count is
	// short.  So lie about it to the caller.
	ret = famez_sendstring(peer_id, msgbody, config);
	if (ret == len)
		ret = spooflen;

unlock_return:
	spin_unlock_bh(&config->scratch_lock);
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
