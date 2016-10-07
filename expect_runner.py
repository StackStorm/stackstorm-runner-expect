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
import re
import json
import grako

import paramiko

from st2common.runners import ActionRunner
from st2common import log as logging
from st2common.constants.action import LIVEACTION_STATUS_SUCCEEDED
from st2common.constants.action import LIVEACTION_STATUS_FAILED
# from st2common.constants.action import LIVEACTION_STATUS_TIMED_OUT

LOG = logging.getLogger(__name__)

HANDLER = 'ssh'

HANDLERS = {}


def get_runner():
    return ExpectRunner(str(uuid.uuid4()))


def _parse_grako(output, grammar, entry):
    parser = grako.genmodel("output_parser", grammar)
    parsed_output = parser.parse(output, entry)
    LOG.info('Parsed output: %s', parsed_output)

    return parsed_output


class ExpectRunner(ActionRunner):
    def __init__(self, runner_id):
        super(ExpectRunner, self).__init__(runner_id=runner_id)
        self._timeout = 60

    def _get_shell_output(self):
        output = ''
        for command in self._cmd:
            cmd = command[0]
            expect = command[1]
            LOG.debug("Dispatching command: %s, %s", cmd, expect)

            output += self._shell.send(cmd, expect)

        return output

    def _init_shell(self):
        LOG.debug('Entering _init_shell')

        self._shell.send('term len 0\n', r'>')

    def pre_run(self):
        super(ExpectRunner, self).pre_run()

        LOG.debug('Entering ExpectRunner.PRE_run() for liveaction_id="%s"',
                  self.liveaction_id)
        self._username = self.runner_parameters.get('username', None)
        self._password = self.runner_parameters.get('password', None)
        self._host = self.runner_parameters.get('host', None)
        self._cmd = self.runner_parameters.get('cmd', None)
        self._entry = self.runner_parameters.get('entry', None)
        self._grammar = self.runner_parameters.get('grammar', None)

    def run(self, action_parameters):
        LOG.debug('Entering ExpectRunner.PRE_run() for liveaction_id="%s"',
                  self.liveaction_id)

        try:
            handler = HANDLERS[HANDLER]

            self._shell = handler(
                self._host,
                self._username,
                self._password,
                self._timeout)
            self._init_shell()

            output = self._get_shell_output()
            parsed_output = _parse_grako(output, self._grammar, self._entry)
            result = json.dumps(parsed_output)

        except Exception, error:
            error_message = dict(error=error)
            err = json.dumps(error_message)

            return (LIVEACTION_STATUS_FAILED, err, None)

        return (LIVEACTION_STATUS_SUCCEEDED, result, None)


class ConnectionHandler(object):
    def send(self, command, expect):
        pass


class SSHHandler(ConnectionHandler):
    def __init__(self, host, username, password, timeout):
        self._ssh = paramiko.SSHClient()
        self._ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self._ssh.connect(host, username=username, password=password)
        self._shell = self._ssh.invoke_shell()
        self._shell.settimeout(timeout)

        while not self._shell.recv_ready():
            time.sleep(.2)

        while self._shell.recv_ready():
            LOG.debug("Captured init message: %s", self._shell.recv(1024))

    def send(self, command, expect):
        LOG.debug('Entering _get_ssh_output')

        self._shell.send(command + "\n")

        return_val = ""
        while re.search(expect, return_val) is None:
            if not self._shell.recv_ready():
                time.sleep(.2)
                continue

            return_val += self._shell.recv(1024)

        LOG.debug('Output: %s', return_val)
        return_val = return_val.replace('\\n', '\n').replace('\\r', '')

        return return_val

HANDLERS['ssh'] = SSHHandler
