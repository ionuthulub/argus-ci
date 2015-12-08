# Copyright 2014 Cloudbase Solutions Srl
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

"""Windows cloudbaseinit recipes."""

import ntpath
import os
import re
import socket
try:
    import urlparse
except ImportError:
    import urllib.parse as urlparse

import six
from winrm import exceptions as winrm_exceptions

from argus import exceptions
from argus.introspection.cloud import windows as introspection
from argus.recipes.cloud import base
from argus import util

LOG = util.get_logger()

# Default values for an instance under booting step.
COUNT = 20
DELAY = 20


class CloudbaseinitRecipe(base.BaseCloudbaseinitRecipe):
    """Recipe for preparing a Windows instance."""
    _invoke_webrequest_cmd = 'Invoke-WebRequest'

    def wait_for_boot_completion(self):
        LOG.info("Waiting for boot completion...")

        wait_cmd = ('net user "{0}"'.format(self._conf.openstack.image_username))

        def boot_completed(stdout, username=self._conf.openstack.image_username):
            if re.match('User name\s*{0}'.format(username), stdout):
                return True
            return False

        self._execute_until_condition(
            wait_cmd,
            boot_completed,
            count=COUNT, delay=DELAY)

    def execution_prologue(self):
        # Nano server does not support Invoke-WebRequest so we need to check
        # if the command exists and add it if not
        stdout = self._execute('powershell Invoke-WebRequest')
        if 'CommandNotFoundException' in stdout:
            target_dir = 'C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\' \
                         'Modules\\InvokeFastWebRequest'
            self._execute(r'mkdir {0}'.format(target_dir))
            target_file = os.path.join(target_dir, 'InvokeFastWebRequest.psm1')
            self._copy_resource('windows/InvokeFastWebRequest.psm1', target_file)
            self.__class__._invoke_webrequest_cmd = 'InvokeFastWebRequest'

        LOG.info("Retrieve common module for proper script execution.")
        self._download_resource('windows/common.psm1', 'C:\\common.psm1')

        self._download_resource('windows/network_details.ps1',
                                'C:\\network_details.ps1')

    def get_installation_script(self):
        """Get an insallation script for CloudbaseInit."""
        LOG.info("Retrieve an installation script for CloudbaseInit.")
        self._download_resource('windows/installCBinit.ps1', 'C:\\installcbinit.ps1')

    def install_cbinit(self, service_type):
        """Run the installation script for CloudbaseInit."""
        installer = "CloudbaseInitSetup_{build}_{arch}.msi".format(
            build=self._conf.argus.build,
            arch=self._conf.argus.arch
        )
        # TODO(cpopa): the service type is specific to each scenario,
        # find a way to pass it
        LOG.info("Run the downloaded installation script "
                 "using the installer %r with service %r.",
                 installer, service_type)

        cmd = ('powershell "C:\\\\installcbinit.ps1 -serviceType {} '
               '-installer {}"'.format(service_type, installer))
        try:
            self._execute(cmd, count=5, delay=5)
        except exceptions.ArgusError:
            # This can happen for multiple reasons,
            # but one of them is the fact that the installer
            # can't be installed through WinRM on some OSes
            # for whatever reason. In this case, we're falling back
            # to use a scheduled task.
            self._deploy_using_scheduled_task(installer, service_type)

        self._grab_cbinit_installation_log()

    def _deploy_using_scheduled_task(self, installer, service_type):
        self._download_resource('windows/schedule_installer.bat',
                                'C:\\schedule_installer.bat')

        # Now run it.
        cmd = ("C:\\\\schedule_installer.bat {0} {1}"
               .format(service_type, installer))
        self._execute(cmd)

    def _grab_cbinit_installation_log(self):
        """Obtain the installation logs."""
        LOG.info("Obtaining the installation logs.")
        if not self._conf.argus.output_directory:
            LOG.warning("The output directory wasn't given, "
                        "the log will not be grabbed.")
            return

        content = self._backend.remote_client.read_file("C:\\installation.log")
        log_template = "installation-{}.log".format(
            self._backend.instance_server()['id'])

        path = os.path.join(self._conf.argus.output_directory, log_template)
        with open(path, 'w') as stream:
            stream.write(content)

    def replace_install(self):
        """Replace the cb-init installed files with the downloaded ones.

        For the same file names, there will be a replace. The new ones
        will just be added and the other files will be left there.
        So it's more like an update.
        """
        link = self._conf.argus.patch_install
        if not link:
            return

        LOG.info("Replacing cloudbaseinit's files...")

        LOG.debug("Download and extract installation bundle.")
        if link.startswith("\\\\"):
            cmd = 'copy "{}" "C:\\install.zip"'.format(link)
            self._execute(cmd)
        else:
            self._download_file(link, 'C:\\install.zip')

        cmds = [
            "Add-Type -A System.IO.Compression.FileSystem",
            "[IO.Compression.ZipFile]::ExtractToDirectory("
            "'C:\\install.zip', 'C:\\install')"
        ]
        cmd = 'powershell {}'.format("; ".join(cmds))
        self._execute(cmd)

        LOG.debug("Replace old files with the new ones.")
        cbdir = introspection.get_cbinit_dir(self._execute)
        self._execute('xcopy /y /e /q "C:\\install\\Cloudbase-Init"'
                      ' "{}"'.format(cbdir))

    def replace_code(self):
        """Replace the code of cloudbaseinit."""
        if not self._conf.argus.git_command:
            # Nothing to replace.
            return

        LOG.info("Replacing cloudbaseinit's code...")

        LOG.info("Getting cloudbase-init location...")
        # Get cb-init python location.
        python_dir = introspection.get_python_dir(self._execute)

        # Remove everything from the cloudbaseinit installation.
        LOG.info("Removing recursively cloudbaseinit...")
        cloudbaseinit = ntpath.join(
            python_dir,
            "Lib",
            "site-packages",
            "cloudbaseinit")
        self._execute('rmdir "{}" /S /q'.format(cloudbaseinit))

        # Clone the repo
        LOG.info("Cloning the cloudbaseinit repo...")
        self._execute("git clone https://github.com/stackforge/"
                      "cloudbase-init C:\\cloudbaseinit")

        # Run the command provided at cli.
        LOG.info("Applying cli patch...")
        self._execute("cd C:\\cloudbaseinit && {}".format(
            self._conf.argus.git_command))

        # Replace the code, by moving the code from cloudbaseinit
        # to the installed location.
        LOG.info("Replacing code...")
        self._execute('powershell "Copy-Item C:\\cloudbaseinit\\cloudbaseinit '
                      '\'{}\' -Recurse"'.format(cloudbaseinit))

        # Autoinstall packages from the new requirements.txt
        python = ntpath.join(python_dir, "python.exe")
        command = '"{}" -m pip install -r C:\\cloudbaseinit\\requirements.txt'
        self._execute(command.format(python))

    def pre_sysprep(self):
        """Disable first_logon_behaviour for testing purposes.

        Because first_logon_behaviour will control how the password
        should work on next logon, we could have troubles in tests,
        so this is always disabled, excepting tests which sets
        it manual to whatever they want.
        """
        introspection.set_config_option(
            option="first_logon_behaviour", value="no",
            execute_function=self._execute)

        # Patch the installation of cloudbaseinit in order to create
        # a file when the execution ends. We're doing this instead of
        # monitoring the service, because on some OSes, just checking
        # if the service is stopped leads to errors, due to the
        # fact that the service starts later on.
        python_dir = introspection.get_python_dir(self._execute)
        cbinit = ntpath.join(python_dir, 'Lib', 'site-packages',
                             'cloudbaseinit')

        # Get the shell patching script and patch the installation.
        self._download_resource('windows/patch_shell.ps1', 'C:\\patch_shell.ps1')

        escaped = introspection.escape_path(cbinit)
        self._execute('powershell "C:\\\\patch_shell.ps1 \"{}\""'
                      .format(escaped))

    def sysprep(self):
        """Prepare the instance for the actual tests, by running sysprep."""
        LOG.info("Running sysprep...")

        self._download_resource('windows/sysprep.ps1', 'C:\\sysprep.ps1')
        try:
            self._backend.remote_client.run_command(
                'powershell C:\\sysprep.ps1')
        except (socket.error, winrm_exceptions.WinRMTransportError):
            # This error is to be expected because the vm will restart
            # before sysprep.ps1 finishes execution.
            # Any other error should propagate.
            pass

    def _wait_cbinit_finalization(self, searched_paths=None):
        """Wait for the finalization of CloudbaseInit.

        The function waits until cloudbaseinit finished.
        """
        LOG.info("Waiting for the finalization of CloudbaseInit execution...")

        # Check if the service actually started.
        test_cmd = 'powershell "Test-Path {} 1> C:\output.txt 2>&1" >null ' \
                   '&& type C:\output.txt'
        check_cmds = [test_cmd.format(introspection.escape_path(path))
                      for path in searched_paths or []]
        for check_cmd in check_cmds:
            self._execute_until_condition(
                check_cmd,
                lambda out: out.strip() == 'True',
                count=COUNT, delay=DELAY)

        # Check if the service finished
        wait_cmd = ('powershell "(Get-Service `"| where -Property Name '
                    '-match cloudbase-init`").Status '
                    '1> C:\output.txt 2>&1" >null && type C:\output.txt')
        self._execute_until_condition(
            wait_cmd,
            lambda out: out.strip() == 'Stopped',
            count=COUNT, delay=DELAY)

    def wait_cbinit_finalization(self):
        paths = [
            "C:\\cloudbaseinit_unattended",
            "C:\\cloudbaseinit_normal"]
        self._wait_cbinit_finalization(searched_paths=paths)

    def _download_file(self, url, path):
        cmd = ("powershell {0} -uri {1} -outfile {2}"
               .format(self._invoke_webrequest_cmd, url, path))
        self._execute(cmd)

    def _download_resource(self, resource, path):
        resource_url = urlparse.urljoin(self._conf.argus.resources, resource)
        self._download_file(resource_url, path)

    def _copy_resource(self, resource, remote_path):
        """Copies a resource from the resources folder to the remote host"""
        resource_content = util.get_resource(resource)

        # escape "
        resource_content = resource_content.replace('"', '^"')
        # escape &
        resource_content = resource_content.replace('&', '^&')
        # escape )
        resource_content = resource_content.replace(')', '^)')
        # replace \n with &echo.
        resource_content = resource_content.replace('\n', '&echo.')

        command = '(echo.' + resource_content + ')'
        self._execute('> {0} {1}'.format(remote_path, command))

class CloudbaseinitScriptRecipe(CloudbaseinitRecipe):
    """A recipe which adds support for testing .exe scripts."""

    def pre_sysprep(self):
        super(CloudbaseinitScriptRecipe, self).pre_sysprep()
        LOG.info("Doing last step before sysprepping.")

        self._download_resource('windows/test_exe.exe', 'C:\\Scripts\\test_exe.exe')


class CloudbaseinitCreateUserRecipe(CloudbaseinitRecipe):
    """A recipe for creating the user created by cloudbaseinit.

    The purpose is to use this recipe for testing that cloudbaseinit
    works, even when the user which should be created already exists.
    """

    def pre_sysprep(self):
        super(CloudbaseinitCreateUserRecipe, self).pre_sysprep()
        LOG.info("Creating the user %s...", self._conf.cloudbaseinit.created_user)
        self._download_resource('windows/create_user.ps1', 'C:\\create_user.ps1')

        self._execute('powershell "C:\\\\create_user.ps1 -user {}"'.format(
            self._conf.cloudbaseinit.created_user))


class BaseNextLogonRecipe(CloudbaseinitRecipe):
    """Useful for testing the next logon behaviour."""

    behaviour = None

    def pre_sysprep(self):
        super(BaseNextLogonRecipe, self).pre_sysprep()

        introspection.set_config_option(
            option="first_logon_behaviour",
            value=self.behaviour,
            execute_function=self._execute)


class AlwaysChangeLogonPasswordRecipe(BaseNextLogonRecipe):
    """Always change the password at next logon."""

    behaviour = 'always'


class ClearPasswordLogonRecipe(BaseNextLogonRecipe):
    """Change the password at next logon if the password is from metadata."""

    behaviour = 'clear_text_injected_only'


class CloudbaseinitMockServiceRecipe(CloudbaseinitRecipe):
    """A recipe for patching the cloudbaseinit's conf with a custom server."""

    config_entry = None
    pattern = "{}"

    def pre_sysprep(self):
        super(CloudbaseinitMockServiceRecipe, self).pre_sysprep()
        LOG.info("Inject guest IP for mocked service access.")

        # Append service IP as a config option.
        address = self.pattern.format(util.get_local_ip())
        introspection.set_config_option(option=self.config_entry,
                                        value=address,
                                        execute_function=self._execute)


class CloudbaseinitEC2Recipe(CloudbaseinitMockServiceRecipe):
    """Recipe for EC2 metadata service mocking."""

    config_entry = "ec2_metadata_base_url"
    pattern = "http://{}:2000/"


class CloudbaseinitCloudstackRecipe(CloudbaseinitMockServiceRecipe):
    """Recipe for Cloudstack metadata service mocking."""

    config_entry = "cloudstack_metadata_ip"
    pattern = "{}:2001"

    def pre_sysprep(self):
        super(CloudbaseinitCloudstackRecipe, self).pre_sysprep()

        python_dir = introspection.get_python_dir(self._execute)
        cbinit = ntpath.join(python_dir, 'Lib', 'site-packages',
                             'cloudbaseinit')

        # Install mock
        python = ntpath.join(python_dir, "python.exe")
        command = '"{}" -m pip install mock'
        self._execute(command.format(python))

        # Get the cloudstack patching script and patch the installation.
        self._download_resource('windows/patch_cloudstack.ps1',
                                'C:\\patch_cloudstack.ps1')

        escaped = introspection.escape_path(cbinit)
        self._execute('powershell "C:\\\\patch_cloudstack.ps1 \"{}\""'
                      .format(escaped))


class CloudbaseinitMaasRecipe(CloudbaseinitMockServiceRecipe):
    """Recipe for Maas metadata service mocking."""

    config_entry = "maas_metadata_url"
    pattern = "http://{}:2002"

    def pre_sysprep(self):
        super(CloudbaseinitMaasRecipe, self).pre_sysprep()

        required_fields = (
            "maas_oauth_consumer_key",
            "maas_oauth_consumer_secret",
            "maas_oauth_token_key",
            "maas_oauth_token_secret",
        )

        for field in required_fields:
            introspection.set_config_option(option=field, value="secret",
                                            execute_function=self._execute)


class CloudbaseinitWinrmRecipe(CloudbaseinitCreateUserRecipe):
    """A recipe for testing the WinRM configuration plugin."""

    def pre_sysprep(self):
        super(CloudbaseinitWinrmRecipe, self).pre_sysprep()
        introspection.set_config_option(
            option="plugins",
            value="cloudbaseinit.plugins.windows.winrmcertificateauth."
                  "ConfigWinRMCertificateAuthPlugin,"
                  "cloudbaseinit.plugins.windows.winrmlistener."
                  "ConfigWinRMListenerPlugin",
            execute_function=self._execute)


class CloudbaseinitHTTPRecipe(CloudbaseinitMockServiceRecipe):
    """Recipe for http metadata service mocking."""

    config_entry = "metadata_base_url"
    pattern = "http://{}:2003/"


class CloudbaseinitKeysRecipe(CloudbaseinitHTTPRecipe,
                              CloudbaseinitCreateUserRecipe):
    """Recipe that facilitates x509 certificates and public keys testing."""

    def pre_sysprep(self):
        super(CloudbaseinitKeysRecipe, self).pre_sysprep()
        introspection.set_config_option(
            option="plugins",
            value="cloudbaseinit.plugins.windows.createuser."
                  "CreateUserPlugin,"
                  "cloudbaseinit.plugins.windows.setuserpassword."
                  "SetUserPasswordPlugin,"
                  "cloudbaseinit.plugins.common.sshpublickeys."
                  "SetUserSSHPublicKeysPlugin,"
                  "cloudbaseinit.plugins.windows.winrmlistener."
                  "ConfigWinRMListenerPlugin,"
                  "cloudbaseinit.plugins.windows.winrmcertificateauth."
                  "ConfigWinRMCertificateAuthPlugin",
            execute_function=self._execute)


class CloudbaseinitLocalScriptsRecipe(CloudbaseinitRecipe):
    """Recipe for testing local scripts return codes."""

    def pre_sysprep(self):
        super(CloudbaseinitLocalScriptsRecipe, self).pre_sysprep()
        LOG.info("Download reboot-required local script.")

        self._download_resource('windows/reboot.cmd', 'C:\\Scripts\\reboot.cmd')


class CloudbaseinitImageRecipe(CloudbaseinitRecipe):
    """Calibrate already sys-prepared cloudbase-init images."""

    def wait_cbinit_finalization(self):
        cbdir = introspection.get_cbinit_dir(self._execute)
        paths = [ntpath.join(cbdir, "log", name)
                 for name in ["cloudbase-init-unattend.log",
                              "cloudbase-init.log"]]
        self._wait_cbinit_finalization(searched_paths=paths)

    def prepare(self, service_type=None, **kwargs):
        LOG.info("Preparing already syspreped instance...")
        self.execution_prologue()

        if self._conf.argus.pause:
            six.moves.input("Press Enter to continue...")

        self.wait_cbinit_finalization()
        LOG.info("Finished preparing instance.")
