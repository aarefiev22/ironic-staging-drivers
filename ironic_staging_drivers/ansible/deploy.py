#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

"""
Ansible deploy driver
"""

import json
import os
import shlex

from ironic_lib import utils as irlib_utils
from oslo_concurrency import processutils
from oslo_config import cfg
from oslo_log import log
from oslo_utils import excutils
from oslo_utils import strutils
from oslo_utils import units
import retrying
import six
import six.moves.urllib.parse as urlparse
import yaml

from ironic.common import dhcp_factory
from ironic.common import exception
from ironic.common.glance_service import service_utils
from ironic.common.i18n import _
from ironic.common.i18n import _LE
from ironic.common.i18n import _LI
from ironic.common.i18n import _LW
from ironic.common import image_service
from ironic.common import images
from ironic.common import keystone
from ironic.common import states
from ironic.common import utils
from ironic.conductor import rpcapi
from ironic.conductor import task_manager
from ironic.conductor import utils as manager_utils
from ironic.drivers import base
from ironic.drivers.modules import deploy_utils


CONF = cfg.CONF

ansible_opts = [
    cfg.StrOpt('ansible_extra_args',
               help=_('Extra arguments to pass on every '
                      'invocation of Ansible.')),
    cfg.IntOpt('verbosity',
               default=0, min=0, max=4,
               help=_('Set ansible verbosity level. '
                      '4 includes detailed SSH debug logging.'
                      'If set, this setting takes precedence over '
                      'global "debug" config option.')),
    cfg.StrOpt('playbooks_path',
               default=os.path.join(os.path.dirname(__file__), 'playbooks'),
               help=_('Path to playbooks, roles and local inventory.')),
    cfg.StrOpt('config_file_path',
               default=os.path.join(
                   os.path.dirname(__file__), 'playbooks', 'ansible.cfg'),
               help=_('Path to ansible configuration file. If set to empty, '
                      'system default will be used.')),
    cfg.IntOpt('post_deploy_get_power_state_retries',
               default=6,
               help=_('Number of times to retry getting power state to check '
                      'if bare metal node has been powered off after a soft '
                      'power off.')),
    cfg.IntOpt('post_deploy_get_power_state_retry_interval',
               default=5,
               help=_('Amount of time (in seconds) to wait between polling '
                      'power state after trigger soft poweroff.')),
    cfg.IntOpt('extra_memory',
               default=10,
               help=_('The memory amount in MiB: free memory minus image '
                      'size.')),
    cfg.BoolOpt('use_ramdisk_callback',
                default=True,
                help=_('Use callback request from ramdisk for start deploy or'
                       'cleaning.')),
]

CONF.register_opts(ansible_opts, group='ansible')
CONF.import_opt('default_ephemeral_format',
                'ironic.drivers.modules.iscsi_deploy',
                group='pxe')

LOG = log.getLogger(__name__)


DEFAULT_PLAYBOOKS = {
    'deploy': 'deploy.yaml',
    'clean': 'clean.yaml'
}
DEFAULT_CLEAN_STEPS = 'clean_steps.yaml'

REQUIRED_PROPERTIES = {
    'deploy_username': _('Deploy ramdisk username for Ansible. '
                         'This user must have passwordless sudo permissions. '
                         'Default is "ansible".'),
    'deploy_key_file': _('Path to private key file. If not specified, '
                         'default keys for user running ironic-conductor '
                         'process will be used. '
                         'Note that for keys with password, those must be '
                         'pre-loaded into ssh-agent.'),
    'deploy_playbook': _('Name of the Ansible playbook used for deployment. '
                         'Default is %s.') % DEFAULT_PLAYBOOKS['deploy'],
    'clean_playbook': _('Name of the Ansible playbook used for cleaning. '
                        'Default is %s.') % DEFAULT_PLAYBOOKS['clean'],
    'clean_steps_config': _('Name of the file with default cleaning steps '
                            'configuration. Default is %s'
                            ) % DEFAULT_CLEAN_STEPS
}
COMMON_PROPERTIES = REQUIRED_PROPERTIES

DISK_LAYOUT_PARAMS = ('root_gb', 'swap_mb', 'ephemeral_gb')

INVENTORY_FILE = os.path.join(CONF.ansible.playbooks_path, 'inventory')


def _parse_ansible_driver_info(node, action='deploy'):
    user = node.driver_info.get('deploy_username', 'ansible')
    key = node.driver_info.get('deploy_key_file')
    default_playbook = DEFAULT_PLAYBOOKS.get(action)
    playbook = node.driver_info.get('%s_playbook' % action, default_playbook)
    if not playbook:
        raise exception.IronicException(
            message='Failed to set ansible playbook for action %(action)s',
            action=action)
    return playbook, user, key


def _get_configdrive_path(basename):
    return os.path.join(CONF.tempdir, basename + '.cndrive')


# NOTE(yuriyz): this is a copy from agent driver
def build_instance_info_for_deploy(task):
    """Build instance_info necessary for deploying to a node."""
    node = task.node
    instance_info = node.instance_info

    image_source = instance_info['image_source']
    if service_utils.is_glance_image(image_source):
        glance = image_service.GlanceImageService(version=2,
                                                  context=task.context)
        image_info = glance.show(image_source)
        swift_temp_url = glance.swift_temp_url(image_info)
        LOG.debug('Got image info: %(info)s for node %(node)s.',
                  {'info': image_info, 'node': node.uuid})
        instance_info['image_url'] = swift_temp_url
        instance_info['image_checksum'] = image_info['checksum']
        instance_info['image_disk_format'] = image_info['disk_format']
        instance_info['image_container_format'] = (
            image_info['container_format'])
    else:
        try:
            image_service.HttpImageService().validate_href(image_source)
        except exception.ImageRefValidationFailed:
            with excutils.save_and_reraise_exception():
                LOG.error(_LE("Ansible deploy supports only HTTP(S) URLs as "
                              "instance_info['image_source']. Either %s "
                              "is not a valid HTTP(S) URL or "
                              "is not reachable."), image_source)
        instance_info['image_url'] = image_source

    return instance_info


def _get_node_ip(task):
    api = dhcp_factory.DHCPFactory().provider
    ip_addrs = api.get_ip_addresses(task)
    if not ip_addrs:
        raise exception.FailedToGetIPAddressOnPort(_(
            "Failed to get IP address for any port on node %s.") %
            task.node.uuid)
    if len(ip_addrs) > 1:
        error = _("Ansible driver does not support multiple IP addresses "
                  "during deploy or cleaning")
        raise exception.InstanceDeployFailure(reason=error)

    return ip_addrs[0]


# some good code from agent
def _reboot_and_finish_deploy(task):
    wait = CONF.ansible.post_deploy_get_power_state_retry_interval * 1000
    attempts = CONF.ansible.post_deploy_get_power_state_retries + 1

    @retrying.retry(
        stop_max_attempt_number=attempts,
        retry_on_result=lambda state: state != states.POWER_OFF,
        wait_fixed=wait
    )
    def _wait_until_powered_off(task):
        return task.driver.power.get_power_state(task)

    try:
        _wait_until_powered_off(task)
    except Exception as e:
        LOG.warning(_LW('Failed to soft power off node %(node_uuid)s '
                    'in at least %(timeout)d seconds. Error: %(error)s'),
                    {'node_uuid': task.node.uuid,
                     'timeout': (wait * (attempts - 1)) / 1000,
                     'error': e})
    manager_utils.node_power_action(task, states.REBOOT)


def _prepare_extra_vars(host_list, variables=None):
    nodes_var = []
    for node_uuid, ip, user in host_list:
        nodes_var.append(dict(name=node_uuid, ip=ip, user=user))
    extra_vars = dict(ironic_nodes=nodes_var)
    if variables:
        extra_vars.update(variables)
    return extra_vars


def _run_playbook(name, extra_vars, key, tags=None, notags=None):
    """Execute ansible-playbook."""
    playbook = os.path.join(CONF.ansible.playbooks_path, name)
    args = ['ansible-playbook', playbook,
            '-i', INVENTORY_FILE,
            '-e', json.dumps(extra_vars),
            ]

    if CONF.ansible.config_file_path:
        env = ['env', 'ANSIBLE_CONFIG=%s' % CONF.ansible.config_file_path]
        args = env + args

    if tags:
        args.append('--tags=%s' % ','.join(tags))

    if notags:
        args.append('--skip-tags=%s' % ','.join(notags))

    if key:
        args.append('--private-key=%s' % key)

    if CONF.ansible.verbosity:
        args.append('-' + 'v' * CONF.ansible.verbosity)
    elif CONF.debug:
        args.append('-vvvv')

    if CONF.ansible.ansible_extra_args:
        args.extend(shlex.split(CONF.ansible.ansible_extra_args))

    try:
        out, err = utils.execute(*args)
        return out, err
    except processutils.ProcessExecutionError as e:
        raise exception.InstanceDeployFailure(reason=six.text_type(e))


def _calculate_memory_req(task):
    image_source = task.node.instance_info['image_source']
    image_size = images.download_size(task.context, image_source)
    return int(image_size / units.Mi) + CONF.ansible.extra_memory


def _parse_partitioning_info(node):

    info = node.instance_info
    i_info = {}

    i_info['root_gb'] = info.get('root_gb')
    error_msg = _("'root_gb' is missing in node's instance_info")
    deploy_utils.check_for_missing_params(i_info, error_msg)

    i_info['swap_mb'] = info.get('swap_mb', 0)
    i_info['ephemeral_gb'] = info.get('ephemeral_gb', 0)
    err_msg_invalid = _("Cannot validate parameter for deploy. Invalid "
                        "parameter %(param)s. Reason: %(reason)s")

    for param in DISK_LAYOUT_PARAMS:
        try:
            i_info[param] = int(i_info[param])
        except ValueError:
            reason = _("%s is not an integer value.") % i_info[param]
            raise exception.InvalidParameterValue(err_msg_invalid %
                                                  {'param': param,
                                                   'reason': reason})

    partitions = [
        dict(name='root',
             size_mib=1024 * i_info.pop('root_gb'),
             boot='yes',
             swap='no')
    ]
    if i_info['swap_mb']:
        partitions.append(
            dict(name='swap',
                 size_mib=i_info.pop('swap_mb'),
                 boot='no',
                 swap='yes'))

    if i_info['ephemeral_gb'] != 0:
        partitions.append(
            dict(name='ephemeral',
                 size_mib=1024 * i_info.pop('ephemeral_gb'),
                 boot='no',
                 swap='no'))
        i_info['ephemeral_format'] = info.get('ephemeral_format')
        if not i_info['ephemeral_format']:
            i_info['ephemeral_format'] = CONF.pxe.default_ephemeral_format
        preserve_ephemeral = info.get('preserve_ephemeral', False)
        try:
            i_info['preserve_ephemeral'] = (
                strutils.bool_from_string(preserve_ephemeral, strict=True))
        except ValueError as e:
            raise exception.InvalidParameterValue(
                err_msg_invalid % {'param': 'preserve_ephemeral', 'reason': e})
        i_info['preserve_ephemeral'] = (
            'yes' if i_info['preserve_ephemeral'] else 'no')

    i_info['ironic_partitions'] = partitions
    configdrive = info.get('configdrive')
    if configdrive:
        if urlparse.urlparse(configdrive).scheme in ('http', 'https'):
            i_info['configdrive'] = configdrive
        else:
            open(_get_configdrive_path(node.uuid), 'w').write(configdrive)
            i_info['configdrive'] = 'file'

    return i_info


def _prepare_variables(task):
    node = task.node
    i_info = node.instance_info
    variables = {
        'url': i_info['image_url'],
        'mem_req': _calculate_memory_req(task),
        'checksum': i_info.get('image_checksum'),
        'disk_format': i_info.get('image_disk_format'),
        'container_format': i_info.get('image_container_format')
    }
    return {'image': variables}


def _get_clean_steps(task, interface=None, override_priorities=None):
    """Get cleaning steps."""
    clean_steps_file = task.node.driver_info.get('clean_steps',
                                                 DEFAULT_CLEAN_STEPS)
    path = os.path.join(CONF.ansible.playbooks_path, clean_steps_file)
    try:
        with open(path) as f:
            internal_steps = yaml.safe_load(f)
    except Exception as e:
        raise exception.NodeCleaningFailure(node=task.node.uuid, reason=e)

    steps = []
    override = override_priorities or {}
    for params in internal_steps:
        name = params.get('name')
        clean_if = params['interface']
        if interface is not None and interface != clean_if:
            continue
        new_priority = override.get(name)
        priority = (new_priority if new_priority is not None else
                    params['priority'])
        step = {
            'step': name,
            'priority': priority,
            'abortable': params.get('abortable', False),
            'argsinfo': params.get('argsinfo'),
            'interface': clean_if,
            'tags': params.get('tags', [])
        }
        steps.append(step)

    return steps


# taken from agent driver
def _notify_conductor_resume_clean(task):
    LOG.debug('Sending RPC to conductor to resume cleaning for node %s',
              task.node.uuid)
    uuid = task.node.uuid
    rpc = rpcapi.ConductorAPI()
    topic = rpc.get_topic_for(task.node)
    # Need to release the lock to let the conductor take it
    task.release_resources()
    rpc.continue_node_clean(task.context, uuid, topic=topic)


def _build_ramdisk_options(node):
    """Build the ramdisk config options for a node."""
    ironic_api = (CONF.conductor.api_url or
                  keystone.get_service_url()).rstrip('/')

    deploy_options = {
        'deployment_id': node.uuid,
        'ironic_api_url': ironic_api,

    }

    return deploy_options


def _deploy(task, node_address):
    """Internal function for deployment to a node."""
    node = task.node
    LOG.debug('IP of node %(node)s is %(ip)s',
              {'node': node.uuid, 'ip': node_address})
    iwdi = node.driver_internal_info.get('is_whole_disk_image')
    variables = _prepare_variables(task)
    if not iwdi:
        variables.update(_parse_partitioning_info(task.node))
    playbook, user, key = _parse_ansible_driver_info(task.node)
    node_list = [(node.uuid, node_address, user)]
    extra_vars = _prepare_extra_vars(node_list, variables=variables)
    notags = ['parted'] if iwdi else []

    LOG.info(_LI('Starting deploy on node %s'), node.uuid)
    _run_playbook(playbook, extra_vars, key, notags=notags)
    LOG.info(_LI('Ansible complete deploy on node %s'), node.uuid)

    LOG.debug('Rebooting node %s to instance', node.uuid)
    manager_utils.node_set_boot_device(task, 'disk', persistent=True)
    _reboot_and_finish_deploy(task)
    task.driver.boot.clean_up_ramdisk(task)


class AnsibleDeploy(base.DeployInterface):
    """Interface for deploy-related actions."""

    def get_properties(self):
        """Return the properties of the interface."""
        return COMMON_PROPERTIES

    def validate(self, task):
        """Validate the driver-specific Node deployment info."""
        task.driver.boot.validate(task)

        node = task.node
        iwdi = node.driver_internal_info.get('is_whole_disk_image')
        if not iwdi and deploy_utils.get_boot_option(node) == "netboot":
            raise exception.InvalidParameterValue(_(
                "Node %(node)s is configured to use the %(driver)s driver "
                "which does not support netboot.") % {'node': node.uuid,
                                                      'driver': node.driver})

        params = {}
        image_source = node.instance_info.get('image_source')
        params['instance_info.image_source'] = image_source
        error_msg = _('Node %s failed to validate deploy image info. Some '
                      'parameters were missing') % node.uuid
        deploy_utils.check_for_missing_params(params, error_msg)

    @task_manager.require_exclusive_lock
    def deploy(self, task):
        """Perform a deployment to a node."""
        manager_utils.node_power_action(task, states.REBOOT)
        if CONF.ansible.use_ramdisk_callback:
            return states.DEPLOYWAIT

        ip_addr = _get_node_ip(task)
        _deploy(task, ip_addr)
        return states.DEPLOYDONE

    @task_manager.require_exclusive_lock
    def tear_down(self, task):
        """Tear down a previous deployment on the task's node."""
        manager_utils.node_power_action(task, states.POWER_OFF)
        return states.DELETED

    def prepare(self, task):
        """Prepare the deployment environment for this node."""
        node = task.node
        if node.provision_state != states.ACTIVE:
            node.instance_info = build_instance_info_for_deploy(task)
            node.save()
            boot_opt = _build_ramdisk_options(node)
            task.driver.boot.prepare_ramdisk(task, boot_opt)

    def clean_up(self, task):
        """Clean up the deployment environment for this node."""
        task.driver.boot.clean_up_ramdisk(task)
        irlib_utils.unlink_without_raise(
            _get_configdrive_path(task.node.uuid))

    def take_over(self, task):
        pass

    def get_clean_steps(self, task):
        """Get the list of clean steps from the file.

        :param task: a TaskManager object containing the node
        :returns: A list of clean step dictionaries
        """
        new_priorities = {
            'erase_devices': CONF.deploy.erase_devices_priority,
        }
        return _get_clean_steps(task, interface='deploy',
                                override_priorities=new_priorities)

    def execute_clean_step(self, task, step):
        """Execute a clean step.

        :param task: a TaskManager object containing the node
        :param step: a clean step dictionary to execute
        :returns: None
        """
        node = task.node
        playbook, user, key = _parse_ansible_driver_info(
            task.node, action='clean')
        stepname = step['step']
        try:
            ip_addr = node.driver_internal_info['ansible_cleaning_ip']
        except KeyError:
            raise exception.NodeCleaningFailure(node=node.uuid,
                                                reason='undefined node IP '
                                                'addresses')
        node_list = [(node.uuid, ip_addr, user)]
        extra_vars = _prepare_extra_vars(node_list)

        LOG.info(_LI('Starting cleaning step %(step)s on node %(node)s'),
                 {'node': node.uuid, 'step': stepname})
        _run_playbook(playbook, extra_vars, key, tags=step['tags'])
        LOG.info(_LI('Ansible complete cleaning step %(step)s '
                     'on node %(node)s.'),
                 {'node': node.uuid, 'step': stepname})

    def prepare_cleaning(self, task):
        """Boot into the ramdisk to prepare for cleaning.

        :param task: a TaskManager object containing the node
        :raises NodeCleaningFailure: if the previous cleaning ports cannot
                be removed or if new cleaning ports cannot be created
        :returns: None or states.CLEANWAIT for async prepare.
        """
        node = task.node
        deploy_utils.prepare_cleaning_ports(task)
        use_callback = CONF.ansible.use_ramdisk_callback
        boot_opt = _build_ramdisk_options(node) if use_callback else {}
        task.driver.boot.prepare_ramdisk(task, boot_opt)
        manager_utils.node_power_action(task, states.REBOOT)
        if use_callback:
            return states.CLEANWAIT
        ip_addr = _get_node_ip(task)
        LOG.debug('IP of node %(node)s is %(ip)s',
                  {'node': node.uuid, 'ip': ip_addr})
        driver_internal_info = node.driver_internal_info
        driver_internal_info['ansible_cleaning_ip'] = ip_addr
        node.driver_internal_info = driver_internal_info
        node.save()
        playbook, user, key = _parse_ansible_driver_info(
            task.node, action='clean')
        node_list = [(node.uuid, ip_addr, user)]
        extra_vars = _prepare_extra_vars(node_list)

        LOG.info(_LI('Wait ramdisk on node %s for cleaning'), node.uuid)
        _run_playbook(playbook, extra_vars, key, tags=['wait'])
        LOG.info(_LI('Node %s is ready for cleaning'), node.uuid)

    def tear_down_cleaning(self, task):
        """Clean up the PXE and DHCP files after cleaning.

        :param task: a TaskManager object containing the node
        :raises NodeCleaningFailure: if the cleaning ports cannot be
                removed
        """
        node = task.node
        driver_internal_info = node.driver_internal_info
        driver_internal_info.pop('ansible_cleaning_ip', None)
        node.driver_internal_info = driver_internal_info
        node.save()
        manager_utils.node_power_action(task, states.POWER_OFF)
        task.driver.boot.clean_up_ramdisk(task)
        deploy_utils.tear_down_cleaning_ports(task)


class AnsibleVendor(base.VendorInterface):

    def validate(self, task, method, **kwargs):
        """Validate the driver-specific Node deployment info.

        No validation necessary.

        :param task: a TaskManager instance
        :param method: method to be validated
        """
        pass

    def get_properties(self):
        """Return the properties of the interface.

        :returns: dictionary of <property name>:<property description> entries.
        """
        return {}

    @base.passthru(['POST'])
    @task_manager.require_exclusive_lock
    def heartbeat(self, task, **kwargs):
        """Method for ansible ramdisk callback."""
        node = task.node
        LOG.debug('Call back from %(node)s.', {'node': node.uuid})

        try:
            callback_url = kwargs['callback_url']
        except KeyError:
            raise exception.MissingParameterValue(_('For heartbeat operation, '
                                                    '"callback_url" must be '
                                                    'specified.'))
        # use port from netloc?
        address = urlparse.urlparse(callback_url).netloc.split(':')[0]

        if node.provision_state == states.DEPLOYWAIT:
            task.process_event('resume')
            try:
                _deploy(task, address)
            except Exception as e:
                error = _('Deploy failed for node %(node)s: '
                          'Error: %(exc)s') % {'node': node.uuid,
                                               'exc': six.text_type(e)}
                LOG.exception(error)
                deploy_utils.set_failed_state(task, error)

            else:
                LOG.info(_LI('Deployment to node %s done'), node.uuid)
                task.process_event('done')

        elif node.provision_state == states.CLEANWAIT:
            LOG.debug('Node %s just booted to start cleaning.',
                      node.uuid)
            driver_internal_info = node.driver_internal_info
            driver_internal_info['ansible_cleaning_ip'] = address
            node.driver_internal_info = driver_internal_info
            node.save()
            try:
                manager_utils.set_node_cleaning_steps(task)
                _notify_conductor_resume_clean(task)
            except Exception as e:
                error = _('cleaning failed for node %(node)s: '
                          'Error: %(exc)s') % {'node': node.uuid,
                                               'exc': six.text_type(e)}
                LOG.exception(error)
                manager_utils.cleaning_error_handler(task, error)

        else:
            LOG.warning(_LW('Call back from %(node)s in invalid provision '
                            'state %(state)s'),
                        {'node': node.uuid, 'state': node.provision_state})