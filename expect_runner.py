# Licensed to the StackStorm, Inc ('StackStorm') under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import uuid
import time
import socket
import re
import json
import grako

import paramiko

from st2common.runners import ActionRunner
from st2common import log as logging
from st2common.util.config_loader import ContentPackConfigLoader
from st2common.constants.action import LIVEACTION_STATUS_SUCCEEDED
from st2common.constants.action import LIVEACTION_STATUS_FAILED
from st2common.constants.action import LIVEACTION_STATUS_TIMED_OUT

LOG = logging.getLogger(__name__)

HANDLER = 'ssh'

HANDLERS = {}

ENTRY_TIME = None

TIMEOUT = 60

SLEEP_TIMER = 0.2


class TimeoutError(Exception):
    pass


def _elapsed_time():
    return time.time() - ENTRY_TIME


def _check_timer():
    return _elapsed_time() <= TIMEOUT


def _remaining_time():
    return TIMEOUT - _elapsed_time()


def _expect_return(expect, output):
    return re.search(expect, output) is not None


def get_runner():
    return ExpectRunner(str(uuid.uuid4()))


class ExpectRunner(ActionRunner):
    def _parse_grako(self, output):
        parser = grako.genmodel("output_parser", self._grammar)
        parsed_output = parser.parse(output, self._entry)
        LOG.info('Parsed output: %s', parsed_output)

        return parsed_output

    def _get_shell_output(self, cmds):
        output = ''

        if not hasattr(cmds, '__iter__'):
            return None

        for entry in cmds:
            # TODO: fix jinja rendering of complex nesting of types in st2
            cmd = entry[0]
            expect = entry[1] if len(entry) > 1 else self._device_profile['default_expect']
            LOG.debug("Dispatching command: %s, %s", cmd, expect)
            output += self._shell.send(cmd, expect)

        return output

    def _close_shell(self):
        LOG.debug('Terminating shell session')
        self._shell.terminate()

    def pre_run(self):
        super(ExpectRunner, self).pre_run()

        LOG.debug(
            'Entering ExpectRunner.PRE_run() for liveaction_id="%s"',
            self.liveaction_id
        )

        self._device_profile = {
            'init_cmds': None,
            'default_expect': None
        }

        pack = self.get_pack_name()
        user = self.get_user()

        LOG.debug("Parsing config: %s, %s", pack, user)
        config_loader = ContentPackConfigLoader(pack_name=pack, user=user)
        self._device_profile.update(config_loader.get_config())
        LOG.debug("Config: %s", self._device_profile)

        self._username = self.runner_parameters.get('username', None)
        self._password = self.runner_parameters.get('password', None)
        self._host = self.runner_parameters.get('host', None)
        self._cmds = self.runner_parameters.get('cmds', None)
        self._entry = self.runner_parameters.get('entry', None)
        self._grammar = self.runner_parameters.get('grammar', None)
        self._timeout = self.runner_parameters.get('timeout', 60)

        global TIMEOUT
        TIMEOUT = self._timeout

    def run(self, action_parameters):
        LOG.debug(
            'Entering ExpectRunner.run() for liveaction_id="%s"',
            self.liveaction_id
        )

        global ENTRY_TIME
        ENTRY_TIME = time.time()

        try:
            handler = HANDLERS[HANDLER]

            self._shell = handler(
                self._host,
                self._username,
                self._password,
                self._timeout
            )

            init_output = self._get_shell_output(self._device_profile['init_cmds'])
            output = self._get_shell_output(self._cmds)
            self._close_shell()

            if self._grammar:
                parsed_output = self._parse_grako(output)
                result = json.dumps(parsed_output)
            else:
                result = json.dumps({'result': output,
                                     'init_output': init_output})

            result_status = LIVEACTION_STATUS_SUCCEEDED

        except (TimeoutError, socket.timeout) as e:
            LOG.debug("Timed out running action.")
            result_status = LIVEACTION_STATUS_TIMED_OUT
            error_message = dict(
                result=None,
                error='Action failed to complete in %s seconds' % TIMEOUT,
                exit_code=-9
            )
            result = error_message

        except Exception as e:
            LOG.debug("Hit exception running action: %s", e)
            result_status = LIVEACTION_STATUS_FAILED
            error_message = dict(error="%s" % e, result=None)
            result = error_message

        return (result_status, result, None)


class ConnectionHandler(object):
    def send(self, command, expect):
        pass


class SSHHandler(ConnectionHandler):
    def __init__(self, host, username, password, timeout):
        self._ssh = paramiko.SSHClient()
        self._ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self._ssh.connect(
            host,
            username=username,
            password=password,
            timeout=timeout
        )
        self._shell = self._ssh.invoke_shell()
        self._shell.settimeout(_remaining_time())
        self._recv()

    def terminate(self):
        self._shell.close()

    def send(self, command, expect):
        self._shell.settimeout(_remaining_time())
        LOG.debug('Entering _get_ssh_output')

        self._shell.send(command + "\n")

        output = self._recv(expect)

        LOG.debug('Output: %s', output)
        output = output.replace('\\n', '\n').replace('\\r', '')

        return output

    def _recv(self, expect=None):
        return_val = ''

        while not self._shell.recv_ready() and _check_timer():
            time.sleep(SLEEP_TIMER)

        while self._shell.recv_ready() and _check_timer():
            return_val += self._shell.recv(1024)

            if (expect and _expect_return(expect, return_val)) or not expect:
                break

            if not self._shell.recv_ready():
                time.sleep(SLEEP_TIMER)
                continue

        if not return_val and not _check_timer():
            raise TimeoutError()

        return return_val

HANDLERS['ssh'] = SSHHandler
