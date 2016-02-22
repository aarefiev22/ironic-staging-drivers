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

import binascii
import collections
import struct

from ironic.common import exception
import six

from ironic_staging_drivers.common.i18n import _


INTEL_NM_DOMAINS = {
    'platform': 0x00,
    'cpu': 0x01,
    'memory': 0x02,
    'protection': 0x03,
    'io': 0x04
}

INTEL_NM_TRIGGERS = {
    'none': 0x00,
    'temperature': 0x01,
    'power': 0x02,
    'reset': 0x03,
    'boot': 0x04
}

INTEL_NM_CPU_CORRECTION = {
    'auto': 0x00,
    'unagressive': 0x20,
    'aggressive': 0x40,
}

INTEL_NM_STORAGE = {
    'persistent': 0x00,
    'volatile': 0x80,
}

INTEL_NM_ACTIONS = {
    'alert': 0x00,
    'shutdown': 0x01,
}

INTEL_NM_POWER_DOMAIN = {
    'primary': 0x00,
    'secondary': 0x80,
}

INTEL_NM_BOOT_MODE = {
    'power': 0x00,
    'performance': 0x01,
}

INTEL_NM_DAYS = collections.OrderedDict([('monday', 0x01),
                                         ('tuesday', 0x02),
                                         ('wednesday', 0x04),
                                         ('thursday', 0x08),
                                         ('friday', 0x10),
                                         ('saturday', 0x20),
                                         ('sunday', 0x40)])

VERSIONS = {
    0x01: '1.0',
    0x02: '1.5',
    0x03: '2.0',
    0x04: '2.5',
    0x05: '3.0'
}

IPMI_VERSIONS = {
    0x01: '1.0',
    0x02: '2.0',
    0x03: '3.0'
}


def _reverse_dict(d):
    return {v: k for k, v in d.items()}


INTEL_NM_DOMAINS_REV = _reverse_dict(INTEL_NM_DOMAINS)
INTEL_NM_TRIGGERS_REV = _reverse_dict(INTEL_NM_TRIGGERS)
INTEL_NM_CPU_CORRECTION_REV = _reverse_dict(INTEL_NM_CPU_CORRECTION)
INTEL_NM_STORAGE_REV = _reverse_dict(INTEL_NM_STORAGE)
INTEL_NM_ACTIONS_REV = _reverse_dict(INTEL_NM_ACTIONS)
INTEL_NM_POWER_DOMAIN_REV = _reverse_dict(INTEL_NM_POWER_DOMAIN)

# OEM group extension code defined in IPMI spec
INTEL_NM_NETFN = '0x2E'

# Intel manufacturer ID for OEM extension, LS byte first
INTEL_NM_ID = ('0x57', '0x01', '0x00')

# Intel NM commands
INTEL_NM_POLICY_CONTROL = '0xC0'
INTEL_NM_POLICY_SET = '0xC1'
INTEL_NM_POLICY_GET = '0xC2'
INTEL_NM_SUSPEND_SET = '0xC5'
INTEL_NM_SUSPEND_GET = '0xC6'
INTEL_NM_CAPABILITIES_GET = '0xC9'
INTEL_NM_VERSION_GET = '0xCA'


def _handle_parsing_error(func):
    """Decorator for handling errors in raw output data."""
    @six.wraps(func)
    def wrapper(raw_data):
        msg = _('Data from Intel Node Manager %s')

        try:
            return func(raw_data)
        except (IndexError, struct.error):
            raise exception.IPMIFailure(msg % _('has wrong length.'))
        except KeyError:
            raise exception.IPMIFailure(msg % _('is corrupted.'))
        except ValueError:
            raise exception.IPMIFailure(msg % _('cannot be converted.'))

    return wrapper


def _hex(x):
    """Formatting integer as two digit hex value."""
    return '0x{:02X}'.format(x)


def _raw_to_int(raw_data):
    """Converting list of raw hex values as strings to integers."""
    return [int(x, 16) for x in raw_data]


def _bytehex(data):
    """Iterate by one byte with hexlify() output."""
    for i in range(0, len(data), 2):
        yield data[i:i + 2]


def _hexarray(data):
    """Converting binary data to list of hex bytes as strings."""
    return ['0x' + x.decode() for x in _bytehex(binascii.hexlify(data))]


def _append_to_command(cmd, data):
    """Append list or single value to command."""
    if not isinstance(data, (list, tuple)):
        data = [data]
    cmd.extend(data)


def _add_to_dict(data_dict, values, names):
    """Add to dict values with corresponding names."""
    data_dict.update(dict(zip(names, values)))


def _create_command_head(command):
    """Create first part of Intel NM command."""
    cmd = [INTEL_NM_NETFN, command]
    _append_to_command(cmd, INTEL_NM_ID)
    return cmd


def _add_domain_policy_id(cmd, data):
    """Add domain id and policy id to command."""
    _append_to_command(cmd, _hex(INTEL_NM_DOMAINS[data['domain_id']]))
    _append_to_command(cmd, _hex(data['policy_id']))


def _days_compose(days):
    """Converting list of days to binary representation."""
    pattern = 0
    for day in days:
        pattern |= INTEL_NM_DAYS[day]
    return pattern


def _days_parse(pattern):
    """Parse binary data with days of week."""
    return [day for day in INTEL_NM_DAYS if pattern & INTEL_NM_DAYS[day]]


def set_policy(policy):
    """Return hex data for policy set command."""
    # NM defaults
    if 'cpu_power_correction' not in policy:
        policy['cpu_power_correction'] = 'auto'
    if 'storage' not in policy:
        policy['storage'] = 'persistent'
    if policy['policy_trigger'] in ('none', 'boot'):
        policy['trigger_limit'] = 0

    cmd = _create_command_head(INTEL_NM_POLICY_SET)
    _append_to_command(cmd, _hex(INTEL_NM_DOMAINS[policy['domain_id']] |
                       0x10 if policy['enable'] else 0x00))
    _append_to_command(cmd, _hex(policy['policy_id']))
    # 0x10 is policy add flag
    _append_to_command(cmd, _hex(INTEL_NM_TRIGGERS[policy['policy_trigger']] |
                       INTEL_NM_CPU_CORRECTION[policy['cpu_power_correction']]
                       | INTEL_NM_STORAGE[policy['storage']] | 0x10))
    _append_to_command(cmd, _hex(INTEL_NM_ACTIONS[policy['action']] |
                       INTEL_NM_POWER_DOMAIN[policy['power_domain']]))

    if isinstance(policy['target_limit'], int):
        limit = policy['target_limit']
    else:
        mode = 0x00 if policy['target_limit']['boot_mode'] == 'power' else 0x01
        cores_disabled = policy['target_limit']['cores_disabled'] << 1
        limit = mode | cores_disabled

    policy_values = struct.pack('<HIHH', limit, policy['correction_time'],
                                policy['trigger_limit'],
                                policy['reporting_period'])
    _append_to_command(cmd, _hexarray(policy_values))

    return cmd


@_handle_parsing_error
def parse_policy(raw_data):
    """Parse policy data."""
    policy = {}
    raw_int = _raw_to_int(raw_data)

    policy['domain_id'] = INTEL_NM_DOMAINS_REV[raw_int[3] & 0x0F]
    policy['enabled'] = bool(raw_int[3] & 0x10)
    policy['per_domain_enabled'] = bool(raw_int[3] & 0x20)
    policy['global_enabled'] = bool(raw_int[3] & 0x40)
    policy['created_by_nm'] = not bool(raw_int[3] & 0x80)
    policy['policy_trigger'] = INTEL_NM_TRIGGERS_REV[raw_int[4] & 0x0F]
    policy['power_policy'] = bool(raw_int[4] & 0x10)
    power_correction = INTEL_NM_CPU_CORRECTION_REV[raw_int[4] & 0x60]
    policy['cpu_power_correction'] = power_correction
    policy['storage'] = INTEL_NM_STORAGE_REV[raw_int[4] & 0x80]
    policy['action'] = INTEL_NM_ACTIONS_REV[raw_int[5] & 0x01]
    policy['power_domain'] = INTEL_NM_POWER_DOMAIN_REV[raw_int[5] & 0x80]
    policy_values = struct.unpack('<HIHH', bytearray(raw_int[6:]))
    policy_names = ('target_limit', 'correction_time', 'trigger_limit',
                    'reporting_period')
    _add_to_dict(policy, policy_values, policy_names)

    return policy


def set_policy_suspend(suspend):
    """Return hex data for policy suspend set command."""
    cmd = _create_command_head(INTEL_NM_SUSPEND_SET)
    _add_domain_policy_id(cmd, suspend)
    periods = suspend['periods']
    _append_to_command(cmd, _hex(len(periods)))

    for period in periods:
        _append_to_command(cmd, _hex(period['start']))
        _append_to_command(cmd, _hex(period['stop']))
        _append_to_command(cmd, _hex(_days_compose(period['days'])))

    return cmd


@_handle_parsing_error
def parse_policy_suspend(raw_data):
    """Parse policy suspend data."""
    suspends = []
    raw_int = _raw_to_int(raw_data)

    policy_num = raw_int[3]
    for num in range(policy_num):
        base = num * 3 + 4
        suspend = {
            "start": raw_int[base],
            "stop": raw_int[base + 1],
            "days": _days_parse(raw_int[base + 2])
        }
        suspends.append(suspend)

    return suspends


def get_capabilities(data):
    """Return hex data for capabilities get command."""
    cmd = _create_command_head(INTEL_NM_CAPABILITIES_GET)
    _append_to_command(cmd, _hex(INTEL_NM_DOMAINS[data['domain_id']]))
    power_policy = 0x10
    _append_to_command(cmd, _hex(INTEL_NM_TRIGGERS[data['policy_trigger']] |
                       power_policy |
                       INTEL_NM_POWER_DOMAIN[data['power_domain']]))

    return cmd


@_handle_parsing_error
def parse_capabilities(raw_data):
    """Parse capabilities data."""
    capabilities = {}
    raw_int = _raw_to_int(raw_data)

    capabilities['max_policies'] = raw_int[3]
    capabilities_values = struct.unpack('<HHIIHH', bytearray(
                                        raw_int[4:20]))
    capabilities_names = ('max_limit_value', 'min_limit_value',
                          'min_correction_time', 'max_correction_time',
                          'min_reporting_period', 'max_reporting_period')
    _add_to_dict(capabilities, capabilities_values, capabilities_names)
    capabilities['domain_id'] = INTEL_NM_DOMAINS_REV[raw_int[20] & 0x0F]
    power_domain = INTEL_NM_POWER_DOMAIN_REV[raw_int[20] & 0x80]
    capabilities['power_domain'] = power_domain

    return capabilities


def control_policies(control_data):
    """Return hex data for enable or disable policy command."""
    cmd = _create_command_head(INTEL_NM_POLICY_CONTROL)

    enable = control_data['enable']
    scope = control_data['scope']

    if scope == 'global':
        flags = '0x01' if enable else '0x00'
        domain_id = 0
        policy_id = 0
    elif scope == 'domain':
        flags = '0x03' if enable else '0x02'
        domain_id = INTEL_NM_DOMAINS[control_data['domain_id']]
        policy_id = 0
    elif scope == 'policy':
        flags = '0x05' if enable else '0x04'
        domain_id = INTEL_NM_DOMAINS[control_data['domain_id']]
        policy_id = control_data['policy_id']

    _append_to_command(cmd, flags)
    _append_to_command(cmd, _hex(domain_id))
    _append_to_command(cmd, _hex(policy_id))

    return cmd


def get_policy(data):
    """Return hex data for policy get command."""
    cmd = _create_command_head(INTEL_NM_POLICY_GET)
    _add_domain_policy_id(cmd, data)

    return cmd


def remove_policy(data):
    """Return hex data for policy remove command."""
    cmd = _create_command_head(INTEL_NM_POLICY_SET)
    _add_domain_policy_id(cmd, data)
    # first 0 is remove policy, extra will be ignored
    _append_to_command(cmd, ('0x00',) * 12)

    return cmd


def get_policy_suspend(data):
    """Return hex data for policy get suspend command."""
    cmd = _create_command_head(INTEL_NM_SUSPEND_GET)
    _add_domain_policy_id(cmd, data)

    return cmd


def remove_policy_suspend(data):
    """Return hex data for policy remove suspend command."""
    cmd = _create_command_head(INTEL_NM_SUSPEND_SET)
    _add_domain_policy_id(cmd, data)
    # remove suspend
    _append_to_command(cmd, '0x00')

    return cmd


def get_version(data):
    """Return hex data for version get command."""
    cmd = _create_command_head(INTEL_NM_VERSION_GET)

    return cmd


@_handle_parsing_error
def parse_version(raw_data):
    """Parse versions data."""
    version = {}
    raw_int = _raw_to_int(raw_data)

    version['nm'] = VERSIONS.get(raw_int[3], 'unknown')
    version['ipmi'] = IPMI_VERSIONS.get(raw_int[4], 'unknown')
    version['patch'] = str(raw_int[5])
    version['firmware'] = str(raw_int[6]) + '.' + str(raw_int[7])

    return version


# Code below taken from Ceilometer
# Copyright 2014 Intel Corporation.
def parse_slave_and_channel(file_path):
    """Parse the dumped file to get slave address and channel number.

    :param file_path: file path of dumped SDR file.
    :return: slave address and channel number of target device.
    """
    prefix = '5701000d01'
    # According to Intel Node Manager spec, section 4.5, for Intel NM
    # discovery OEM SDR records are type C0h. It contains manufacture ID
    # and OEM data in the record body.
    # 0-2 bytes are OEM ID, byte 3 is 0Dh and byte 4 is 01h. Byte 5, 6
    # is Intel NM device slave address and channel number/sensor owner LUN.
    with open(file_path, 'rb') as bin_fp:
        data_str = binascii.hexlify(bin_fp.read())

    if six.PY3:
        data_str = data_str.decode()
    oem_id_index = data_str.find(prefix)
    if oem_id_index != -1:
        ret = data_str[oem_id_index + len(prefix):
                       oem_id_index + len(prefix) + 4]
        # Byte 5 is slave address. [7:4] from byte 6 is channel
        # number, so just pick ret[2] here.
        return ('0x' + ret[0:2], '0x0' + ret[2])