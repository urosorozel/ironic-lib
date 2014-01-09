# Copyright (c) 2012 NTT DOCOMO, INC.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.


import os
import re
import socket
import stat
import time

from ironic.common import exception
from ironic.common import utils
from ironic.openstack.common import excutils
from ironic.openstack.common import log as logging
from ironic.openstack.common import processutils


LOG = logging.getLogger(__name__)


# All functions are called from deploy() directly or indirectly.
# They are split for stub-out.

def discovery(portal_address, portal_port):
    """Do iSCSI discovery on portal."""
    utils.execute('iscsiadm',
                  '-m', 'discovery',
                  '-t', 'st',
                  '-p', '%s:%s' % (portal_address, portal_port),
                  run_as_root=True,
                  check_exit_code=[0])


def login_iscsi(portal_address, portal_port, target_iqn):
    """Login to an iSCSI target."""
    utils.execute('iscsiadm',
                  '-m', 'node',
                  '-p', '%s:%s' % (portal_address, portal_port),
                  '-T', target_iqn,
                  '--login',
                  run_as_root=True,
                  check_exit_code=[0])
    # Ensure the login complete
    time.sleep(3)


def logout_iscsi(portal_address, portal_port, target_iqn):
    """Logout from an iSCSI target."""
    utils.execute('iscsiadm',
                  '-m', 'node',
                  '-p', '%s:%s' % (portal_address, portal_port),
                  '-T', target_iqn,
                  '--logout',
                  run_as_root=True,
                  check_exit_code=[0])


def delete_iscsi(portal_address, portal_port, target_iqn):
    """Delete the iSCSI target."""
    utils.execute('iscsiadm',
                  '-m', 'node',
                  '-p', '%s:%s' % (portal_address, portal_port),
                  '-T', target_iqn,
                  '-o', 'delete',
                  run_as_root=True,
                  check_exit_code=[0])


def make_partitions(dev, root_mb, swap_mb, ephemeral_mb):
    """Create partitions for root and swap on a disk device."""
    # Lead in with 1MB to allow room for the partition table itself, otherwise
    # the way sfdisk adjusts doesn't shift the partition up to compensate, and
    # we lose the space.
    # http://bazaar.launchpad.net/~ubuntu-branches/ubuntu/raring/util-linux/
    # raring/view/head:/fdisk/sfdisk.c#L1940
    if ephemeral_mb:
        stdin_command = ('1,%d,83;\n,%d,82;\n,%d,83;\n0,0;\n' %
                (ephemeral_mb, swap_mb, root_mb))
    else:
        stdin_command = ('1,%d,83;\n,%d,82;\n0,0;\n0,0;\n' %
                (root_mb, swap_mb))
    utils.execute('sfdisk', '-uM', dev, process_input=stdin_command,
            run_as_root=True,
            attempts=3,
            check_exit_code=[0])
    # avoid "device is busy"
    time.sleep(3)


def is_block_device(dev):
    """Check whether a device is block or not."""
    s = os.stat(dev)
    return stat.S_ISBLK(s.st_mode)


def dd(src, dst):
    """Execute dd from src to dst."""
    utils.execute('dd',
                  'if=%s' % src,
                  'of=%s' % dst,
                  'bs=1M',
                  'oflag=direct',
                  run_as_root=True,
                  check_exit_code=[0])


def mkswap(dev, label='swap1'):
    """Execute mkswap on a device."""
    utils.mkfs('swap', dev, label)


def mkfs_ephemeral(dev, ephemeral_format, label="ephemeral0"):
    utils.mkfs(ephemeral_format, dev, label)


def block_uuid(dev):
    """Get UUID of a block device."""
    out, _ = utils.execute('blkid', '-s', 'UUID', '-o', 'value', dev,
                           run_as_root=True,
                           check_exit_code=[0])
    return out.strip()


def switch_pxe_config(path, root_uuid):
    """Switch a pxe config from deployment mode to service mode."""
    with open(path) as f:
        lines = f.readlines()
    root = 'UUID=%s' % root_uuid
    rre = re.compile(r'\{\{ ROOT \}\}')
    dre = re.compile('^default .*$')
    with open(path, 'w') as f:
        for line in lines:
            line = rre.sub(root, line)
            line = dre.sub('default boot', line)
            f.write(line)


def notify(address, port):
    """Notify a node that it becomes ready to reboot."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.connect((address, port))
        s.send('done')
    finally:
        s.close()


def get_dev(address, port, iqn, lun):
    """Returns a device path for given parameters."""
    dev = "/dev/disk/by-path/ip-%s:%s-iscsi-%s-lun-%s" \
            % (address, port, iqn, lun)
    return dev


def get_image_mb(image_path):
    """Get size of an image in Megabyte."""
    mb = 1024 * 1024
    image_byte = os.path.getsize(image_path)
    # round up size to MB
    image_mb = int((image_byte + mb - 1) / mb)
    return image_mb


def work_on_disk(dev, root_mb, swap_mb, ephemeral_mb,
                 ephemeral_format, image_path):
    """Creates partitions and write an image to the root partition."""

    # NOTE(lucasagomes): When there's an ephemeral partition we want
    # root to be last because that would allow root to resize and make it
    # safer to do takeovernode with slightly larger images
    if ephemeral_mb:
        ephemeral_part = "%s-part1" % dev
        swap_part = "%s-part2" % dev
        root_part = "%s-part3" % dev
    else:
        root_part = "%s-part1" % dev
        swap_part = "%s-part2" % dev

    if not is_block_device(dev):
        raise exception.InstanceDeployFailure(_("Parent device '%s' not found")
                                              % dev)
    make_partitions(dev, root_mb, swap_mb, ephemeral_mb)

    if not is_block_device(root_part):
        raise exception.InstanceDeployFailure(_("Root device '%s' not found")
                                              % root_part)
    if not is_block_device(swap_part):
        raise exception.InstanceDeployFailure(_("Swap device '%s' not found")
                                              % swap_part)
    if ephemeral_mb and not is_block_device(ephemeral_part):
        raise exception.InstanceDeployFailure(
                         _("Ephemeral device '%s' not found") % ephemeral_part)

    dd(image_path, root_part)
    mkswap(swap_part)

    if ephemeral_mb:
        mkfs_ephemeral(ephemeral_part, ephemeral_format)

    try:
        root_uuid = block_uuid(root_part)
    except processutils.ProcessExecutionError:
        with excutils.save_and_reraise_exception():
            LOG.error(_("Failed to detect root device UUID."))
    return root_uuid


def deploy(address, port, iqn, lun, image_path, pxe_config_path,
           root_mb, swap_mb, ephemeral_mb, ephemeral_format):
    """All-in-one function to deploy a node."""
    dev = get_dev(address, port, iqn, lun)
    image_mb = get_image_mb(image_path)
    if image_mb > root_mb:
        root_mb = image_mb
    discovery(address, port)
    login_iscsi(address, port, iqn)
    try:
        root_uuid = work_on_disk(dev, root_mb, swap_mb, ephemeral_mb,
                                 ephemeral_format, image_path)
    except processutils.ProcessExecutionError as err:
        with excutils.save_and_reraise_exception():
            LOG.error(_("Deploy to address %s failed.") % address)
            LOG.error(_("Command: %s") % err.cmd)
            LOG.error(_("StdOut: %r") % err.stdout)
            LOG.error(_("StdErr: %r") % err.stderr)
    except exception.InstanceDeployFailure as e:
        with excutils.save_and_reraise_exception():
            LOG.error(_("Deploy to address %s failed.") % address)
            LOG.error(e)
    finally:
        logout_iscsi(address, port, iqn)
        delete_iscsi(address, port, iqn)
    switch_pxe_config(pxe_config_path, root_uuid)
    # Ensure the node started netcat on the port after POST the request.
    time.sleep(3)
    notify(address, 10000)
