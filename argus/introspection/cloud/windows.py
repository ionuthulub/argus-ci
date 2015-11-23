# Copyright 2015 Cloudbase Solutions Srl
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


import collections
import contextlib
import ntpath
import os
import re
import shutil
import tempfile

from argus.introspection.cloud import base
from argus import exceptions
from argus import util


# escaped characters for powershell paths
ESC = "( )"
SEP = "----\r\n"    # default separator for network details blocks

NIC_KEYS = ["mac", "address", "gateway", "netmask", "dns", "dhcp"]
Address = collections.namedtuple("Address", ["v4", "v6"])
NICDetails = collections.namedtuple("NICDetails", NIC_KEYS)


@contextlib.contextmanager
def _create_tempdir():
    tempdir = tempfile.mkdtemp(prefix="cloudbaseinit-ci-tests")
    try:
        yield tempdir
    finally:
        shutil.rmtree(tempdir)


@contextlib.contextmanager
def _create_tempfile(content=None):
    with _create_tempdir() as temp:
        file_desc, path = tempfile.mkstemp(dir=temp)
        os.close(file_desc)
        if content:
            with open(path, 'w') as stream:
                stream.write(content)
        yield path


def _get_ntp_peers(output):
    peers = []
    for line in output.splitlines():
        if not line.startswith("Peer: "):
            continue
        _, _, entry_peers = line.partition(":")
        peers.extend(entry_peers.split(","))
    return list(filter(None, map(str.strip, peers)))


def escape_path(path):
    """Escape the spaces in the given path in order to work with Powershell properly."""
    for char in ESC:
        path = path.replace(char, "`{}".format(char))
    return path


def _get_ips(ips_as_string):
    """Returns viable v4 and v6 IPs from a space separated string."""
    ips = ips_as_string.split(" ")[1:]    # skip the header
    ips_v4, ips_v6 = [], []
    # There is no guarantee if all the IPs are valid and sorted by type.
    for ip in ips:
        if not ip:
            continue
        if "." in ip and ":" not in ip:
            ips_v4.append(ip)
        else:
            ips_v6.append(ip)
    return ips_v4, ips_v6


def _get_nic_details(details):
    """Get parsed network details from the raw ones."""
    nic_details = dict.fromkeys(NIC_KEYS)
    for detail in details:
        if detail.startswith("mac"):
            nic_details["mac"] = detail.split(" ")[1]
        elif detail.startswith("address"):
            v4s, v6s = _get_ips(detail)
            if len(v6s) >= 2:
                v6 = v6s[1]
            else:
                v6 = None
            nic_details["address"] = Address(v4s[0], v6)
        elif detail.startswith("gateway"):
            v4s, v6s = _get_ips(detail)
            v4 = v4s[0] if v4s else None
            v6 = v6s[0] if v6s else None
            nic_details["gateway"] = Address(v4, v6)
        elif detail.startswith("netmask"):
            # Similar to "address" field.
            v4s, v6s = _get_ips(detail)
            v4 = v4s[0]
            if len(v6s) >= 2:
                v6 = v6s[1]
            else:
                v6 = None
            nic_details["netmask"] = Address(v4, v6)
        elif detail.startswith("dns"):
            v4s, v6s = _get_ips(detail)
            nic_details["dns"] = Address(v4s, v6s)
        elif detail.startswith("dhcp"):
            nic_details["dhcp"] = detail.split(" ")[1].lower() == "true"
    return NICDetails(**nic_details)


def get_cbinit_dir(execute_function):
    """Get the location of cloudbase-init from the instance."""
    stdout = execute_function(
        'powershell "(Get-CimInstance  Win32_OperatingSystem).'
        'OSArchitecture"')
    architecture = stdout.strip()

    locations = [execute_function('powershell "$ENV:ProgramFiles"')]
    if architecture == '64-bit':
        location = execute_function(
            'powershell "${ENV:ProgramFiles(x86)}"')
        locations.append(location)

    for location in locations:
        location = location.strip()
        _location = escape_path(location)
        status = execute_function(
            'powershell Test-Path "{}\\Cloudbase` Solutions"'.format(
                _location)).strip().lower()

        if status == "true":
            return ntpath.join(
                location,
                "Cloudbase Solutions",
                "Cloudbase-Init"
            )

    raise exceptions.ArgusError('cloudbase-init installation dir not found')


def set_config_option(option, value, execute_function):
    """Set the value for the given *option* to *value*."""

    line = "{} = {}".format(option, value)
    cbdir = get_cbinit_dir(execute_function)
    conf = ntpath.join(cbdir, "conf", "cloudbase-init.conf")

    cmd = ('powershell "((Get-Content {0!r}) + {1!r}) |'
           ' Set-Content {0!r}"'.format(conf, line))
    execute_function(cmd)


def get_python_dir(execute_function):
    """Find python directory from the cb-init installation."""
    cbinit_dir = get_cbinit_dir(execute_function)
    command = 'dir "{}" /b'.format(cbinit_dir)
    stdout = execute_function(command).strip()
    names = list(filter(None, stdout.splitlines()))
    for name in names:
        if "python" in name.lower():
            return ntpath.join(cbinit_dir, name)


def get_cbinit_key(execute_function):
    """Get the proper registry key for Cloudbase-init."""
    key = ("HKLM:SOFTWARE\\Cloudbase` Solutions\\"
           "Cloudbase-init")
    key_x64 = ("HKLM:SOFTWARE\\Wow6432Node\\Cloudbase` Solutions\\"
               "Cloudbase-init")
    cmd = 'powershell "Test-Path {}"'.format(key)
    if execute_function(cmd).strip().lower() == "true":
        return key
    return key_x64


class InstanceIntrospection(base.CloudInstanceIntrospection):
    """Utilities for introspecting a Windows instance."""

    def get_disk_size(self):
        cmd = ('powershell (Get-WmiObject "win32_logicaldisk | '
               'where -Property DeviceID -Match C:").Size')
        return int(self.remote_client.run_command_verbose(cmd))

    def username_exists(self, username):
        cmd = ('powershell "Get-WmiObject Win32_Account | '
               'where -Property Name -contains {0}"'
               .format(username))

        stdout = self.remote_client.run_command_verbose(cmd)
        return bool(stdout)

    def get_instance_ntp_peers(self):
        command = 'w32tm /query /peers'
        stdout = self.remote_client.run_command_verbose(command)
        return _get_ntp_peers(stdout)

    def get_instance_keys_path(self):
        cmd = 'echo %cd%'
        stdout = self.remote_client.run_command_verbose(cmd)
        homedir, _, _ = stdout.rpartition(ntpath.sep)
        return ntpath.join(
            homedir, self._conf.cloudbaseinit.created_user,
            ".ssh", "authorized_keys")

    def get_instance_file_content(self, filepath):
        cmd = 'powershell "cat %s"' % filepath
        return self.remote_client.run_command_verbose(cmd)

    def get_userdata_executed_plugins(self):
        cmd = 'powershell "(Get-ChildItem -Path  C:\\ *.txt).Count'
        stdout = self.remote_client.run_command_verbose(cmd)
        return int(stdout)

    @staticmethod
    def _parse_netsh_output(output):
        output = output.strip()
        blocks = re.split(r"SubInterface\s+(.*?)-{46}\s+", output,
                          flags=re.DOTALL)
        blocks = blocks[1:]  # empty space

        interfaces = blocks[0::2]
        content = blocks[1::2]
        for interface, block in zip(interfaces, content):
            interface = interface.strip()
            mtu = re.search(r"MTU\s*:\s*(\d+)\s+", block)
            if not mtu:
                continue
            if 'loopback' not in interface.lower():
                yield mtu.group(1)

    def get_instance_mtu(self):
        cmd = 'netsh interface ipv4 show subinterfaces level=verbose'
        stdout = self.remote_client.run_command_verbose(cmd)
        return next(self._parse_netsh_output(stdout), None)

    def get_cloudbaseinit_traceback(self):
        code = util.get_resource('windows/get_traceback.ps1')
        remote_script = "C:\\{}.ps1".format(util.rand_name())
        with _create_tempfile(content=code) as tmp:
            self.remote_client.copy_file(tmp, remote_script)
            stdout = self.remote_client.run_command_verbose(
                "powershell " + remote_script)
            return stdout.strip()

    def _file_exist(self, filepath):
        stdout = self.remote_client.run_command_verbose(
            'powershell "Test-Path {}"'.format(filepath))
        return stdout.strip() == 'True'

    def instance_exe_script_executed(self):
        return self._file_exist("C:\\Scripts\\exe.output")

    def get_group_members(self, group):
        cmd = "net localgroup {}".format(group)
        std_out = self.remote_client.run_command_verbose(cmd)
        member_search = re.search(
            r"Members\s+-+\s+(.*?)The\s+command",
            std_out, re.MULTILINE | re.DOTALL)
        if not member_search:
            raise ValueError('Unable to get members.')

        return list(filter(None, member_search.group(1).split()))

    def list_location(self, location):
        command = "dir {} /b".format(location)
        stdout = self.remote_client.run_command_verbose(command)
        return list(filter(None, stdout.splitlines()))

    def get_service_triggers(self, service):
        """Get the triggers of the given service.

        Return a tuple of two elements, where the first is the start
        trigger and the second is the end trigger.
        """
        command = "sc qtriggerinfo {}".format(service)
        stdout = self.remote_client.run_command_verbose(command)
        match = re.search(r"START SERVICE\s+(.*?):.*?STOP SERVICE\s+(.*?):",
                          stdout, re.DOTALL)
        if not match:
            raise ValueError("Unable to get the triggers for the "
                             "given service.")
        return (match.group(1).strip(), match.group(2).strip())

    def get_instance_os_version(self):
        """Get the version of the underlying OS

         Return a tuple of two elements, the major and the minor
         version.
        """
        cmd = "powershell (Get-CimInstance Win32_OperatingSystem).Version"
        stdout = self.remote_client.run_command_verbose(cmd)
        elems = stdout.split(".")
        return list(map(int, elems))[:2]

    def get_cloudconfig_executed_plugins(self):
        expected = {
            'b64', 'b64_1',
            'gzip', 'gzip_1',
            'gzip_base64', 'gzip_base64_1', 'gzip_base64_2'
        }
        files = {}
        for basefile in expected:
            path = ntpath.join("C:\\", basefile)
            content = self.get_instance_file_content(path)
            files[basefile] = content.strip()
        return files

    def get_timezone(self):
        command = "[System.TimeZone]::CurrentTimeZone.StandardName"
        stdout = self.remote_client.run_command_verbose(
            "powershell {}".format(command))
        return stdout

    def get_instance_hostname(self):
        command = "hostname"
        stdout = self.remote_client.run_command_verbose(command)
        return stdout.lower().strip()

    def get_network_interfaces(self):
        """Get a list with dictionaries of network details.

        If a value is an empty string, then that value is missing.
        """
        cmd = ("powershell Invoke-WebRequest -uri "
               "{}/windows/network_details.ps1 -outfile "
               "C:\\network_details.ps1".format(self._conf.argus.resources))
        self.remote_client.run_command_with_retry(cmd)

        # Run and parse the output, where each adapter details
        # block is separated by a specific separator.
        # Each block contains multiple fields separated by EOLs
        # and each field contains multiple details separated by spaces.
        cmd = "powershell C:\\network_details.ps1"
        output = self.remote_client.run_command_verbose(cmd)

        output = output.replace(SEP, "", 1)
        nics = []
        for block in output.split(SEP):
            details = block.strip().splitlines()
            if len(details) < 6:
                continue    # not enough, invalid data block
            # Must follow `argus.util.NETWORK_KEYS` model.
            nic_details = _get_nic_details(details)
            nic = {
                "mac": nic_details.mac,
                "address": nic_details.address.v4,
                "address6": nic_details.address.v6,
                "gateway": nic_details.gateway.v4,
                "gateway6": nic_details.gateway.v6,
                "netmask": nic_details.netmask.v4,
                "netmask6": nic_details.netmask.v6,
                "dns": nic_details.dns.v4,
                "dns6": nic_details.dns.v6,
                "dhcp": nic_details.dhcp
            }
            nics.append(nic)
        return nics

    def get_user_flags(self, user):
        code = util.get_resource('windows/get_user_flags.ps1')
        remote_script = "C:\\{}.ps1".format(util.rand_name())
        with _create_tempfile(content=code) as tmp:
            self.remote_client.copy_file(tmp, remote_script)
            stdout = self.remote_client.run_command_verbose(
                "powershell {0} {1}".format(remote_script, user))
            return stdout.strip()
